"""
Signal Server - Multi-Exchange Executor
Supports: Binance, OKX, Bybit, Bitget, Gate.io, Coinbase
Enhanced with multi-TP and trailing-stop execution
"""
import asyncio
import hashlib as _hashlib
import inspect
import threading as _threading
import time
from typing import Any

from loguru import logger

from core.config import settings
from models import SignalDirection, TradeDecision, TrailingStopMode

try:
    import ccxt
    _CCXT_AVAILABLE = True
except ModuleNotFoundError:
    _CCXT_AVAILABLE = False

    class _MissingCCXT:
        class Exchange:
            pass

        class InsufficientFunds(Exception):
            pass

        class NetworkError(Exception):
            pass

        class AuthenticationError(Exception):
            pass

        class OrderNotFound(Exception):
            pass

        binance = okx = bybit = bitget = gate = coinbase = None

    ccxt = _MissingCCXT()


async def _close_exchange(exchange):
    close = getattr(exchange, "close", None)
    if not close:
        return
    result = await asyncio.to_thread(close)
    if inspect.isawaitable(result):
        await result


_MISSING = object()


def _credential_value(value: object = _MISSING, fallback: str = "") -> str:
    """Preserve explicit empty credentials instead of falling back to globals."""
    if value is _MISSING:
        return str(fallback or "")
    if value is None:
        return ""
    return str(value)


def _credential_from_exchange_config(exchange_config: dict[str, Any], key: str, fallback: str = "") -> str:
    """Resolve a credential from config while preserving explicit empty values."""
    if key in exchange_config:
        return _credential_value(exchange_config.get(key))
    return _credential_value(_MISSING, fallback)


def _is_order_not_found_error(exc: Exception) -> bool:
    """Best-effort detection for exchanges that raise generic not-found errors."""
    if isinstance(exc, getattr(ccxt, "OrderNotFound", Exception)):
        return True
    return "not found" in str(exc).lower()


def _is_okx_pos_side_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "posside error" in text or ('"scode":"51000"' in text and "posside" in text)


def _exchange_id(exchange: ccxt.Exchange) -> str:
    return str(getattr(exchange, "id", "") or "").lower().strip()


def _okx_position_side(side: str) -> str:
    return "long" if str(side).lower() == "buy" else "short"


def _order_create_attempts(exchange, side: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    base = dict(params or {})
    if _exchange_id(exchange) != "okx":
        return [base]

    pos_side = _okx_position_side(side)
    return [
        {**base, "tdMode": base.get("tdMode") or "cross"},
        {**base, "tdMode": base.get("tdMode") or "cross", "posSide": pos_side},
    ]


async def _create_exchange_order(
    exchange,
    symbol: str,
    order_type: str,
    side: str,
    amount: float,
    price: float | None = None,
    params: dict[str, Any] | None = None,
) -> dict:
    """Create an exchange order with small exchange-specific retries."""
    errors: list[str] = []
    for attempt_params in _order_create_attempts(exchange, side, params):
        try:
            if price is None:
                return await asyncio.to_thread(
                    exchange.create_order,
                    symbol=symbol,
                    type=order_type,
                    side=side,
                    amount=amount,
                    params=attempt_params,
                )
            return await asyncio.to_thread(
                exchange.create_order,
                symbol=symbol,
                type=order_type,
                side=side,
                amount=amount,
                price=price,
                params=attempt_params,
            )
        except Exception as exc:
            errors.append(f"{attempt_params}: {exc}")
            if not (_exchange_id(exchange) == "okx" and _is_okx_pos_side_error(exc)):
                break
    raise RuntimeError("; ".join(errors[-2:]) or f"Failed to create {order_type} order")


# ─────────────────────────────────────────────
# Exchange instance cache (#19)
# Reuse CCXT instances for the same exchange+sandbox+credentials config
# to avoid repeated connection setup overhead.
# ─────────────────────────────────────────────
_exchange_pool: dict[str, ccxt.Exchange] = {}
_exchange_pool_lock = _threading.Lock()
_exchange_pool_health: dict[str, dict[str, Any]] = {}
_HEALTH_CHECK_INTERVAL_SECS = 300
_MAX_CONSECUTIVE_FAILURES = 3


def _get_or_create_exchange(
    exchange_id: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    password: str = "",
    live: bool = False,
    sandbox: bool | None = None,
    market_type: str | None = None,
) -> ccxt.Exchange:
    """Return a cached CCXT instance or create a new one.

    Uses double-checked locking pattern to avoid race conditions
    while minimizing lock contention.

    Includes health check to evict stale/unhealthy connections.
    """
    eid = (exchange_id or settings.exchange.name).lower().strip()
    cred_hash = _hashlib.sha256(f"{api_key}:{api_secret}:{password}".encode()).hexdigest()[:8]
    sb = settings.exchange.sandbox_mode if sandbox is None else bool(sandbox)
    market_key = str(market_type or settings.exchange.market_type or "contract").lower().strip()
    cache_key = f"{eid}:{sb}:{market_key}:{cred_hash}"

    existing = _exchange_pool.get(cache_key)
    if existing is not None:
        health = _exchange_pool_health.get(cache_key, {})
        now = time.time()
        last_check = health.get("last_check", 0)
        consecutive_failures = health.get("consecutive_failures", 0)

        if consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
            logger.warning(f"[Exchange] Evicting unhealthy cached instance: {cache_key}")
            _exchange_pool.pop(cache_key, None)
            _exchange_pool_health.pop(cache_key, None)
            try:
                close = getattr(existing, "close", None)
                if close:
                    close()
            except Exception:
                pass
        elif now - last_check > _HEALTH_CHECK_INTERVAL_SECS:
            try:
                existing.fetch_time()
                _exchange_pool_health[cache_key] = {
                    "last_check": now,
                    "consecutive_failures": 0,
                }
                return existing
            except Exception as exc:
                logger.warning(f"[Exchange] Health check failed for {cache_key}: {exc}")
                _exchange_pool_health[cache_key] = {
                    "last_check": now,
                    "consecutive_failures": consecutive_failures + 1,
                }
                if consecutive_failures + 1 >= _MAX_CONSECUTIVE_FAILURES:
                    _exchange_pool.pop(cache_key, None)
                    _exchange_pool_health.pop(cache_key, None)
                    try:
                        close = getattr(existing, "close", None)
                        if close:
                            close()
                    except Exception:
                        pass
                else:
                    return existing
        else:
            return existing

    with _exchange_pool_lock:
        existing = _exchange_pool.get(cache_key)
        if existing is not None:
            return existing

        instance = _build_exchange(exchange_id, api_key, api_secret, password, live, sandbox, market_type)

        if len(_exchange_pool) >= settings.exchange.pool_max_size:
            oldest_key = next(iter(_exchange_pool))
            evicted = _exchange_pool.pop(oldest_key, None)
            _exchange_pool_health.pop(oldest_key, None)
            if evicted is not None:
                try:
                    close = getattr(evicted, "close", None)
                    if close:
                        close()
                except Exception:
                    pass

        _exchange_pool[cache_key] = instance
        _exchange_pool_health[cache_key] = {
            "last_check": time.time(),
            "consecutive_failures": 0,
        }
        return instance


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
    exchange_id: str | None = None,
    api_key: str | None = None,
    api_secret: str | None = None,
    password: str = "",
    live: bool = False,
    sandbox: bool | None = None,
    market_type: str | None = None,
) -> ccxt.Exchange:
    """Build CCXT exchange instance with proper configuration."""
    if not _CCXT_AVAILABLE:
        raise RuntimeError("ccxt is not installed; install project requirements to enable live exchange execution")

    if exchange_id is None:
        exchange_id = settings.exchange.name
    exchange_id = exchange_id.lower().strip()

    if exchange_id not in SUPPORTED_EXCHANGES:
        raise ValueError(f"Unsupported exchange: {exchange_id}")

    config = SUPPORTED_EXCHANGES[exchange_id]
    exchange_class = config["class"]
    selected_market_type = str(market_type or settings.exchange.market_type or "contract").lower().strip()
    options: dict[str, object] = dict(config.get("futures_option", {}))
    if selected_market_type == "spot":
        options["defaultType"] = "spot"

    # Build exchange config
    resolved_api_key = _credential_value(api_key, settings.exchange.api_key)
    resolved_api_secret = _credential_value(api_secret, settings.exchange.api_secret)
    resolved_password = _credential_value(password, settings.exchange.password)

    exchange_config: dict[str, object] = {
        "apiKey": resolved_api_key,
        "secret": resolved_api_secret,
        "enableRateLimit": True,
        "options": options,
    }

    # Add password for exchanges that require it
    if resolved_password or "password" in (config.get("extra_keys") or []):
        exchange_config["password"] = resolved_password

    # Create exchange instance
    exchange = exchange_class(exchange_config)

    sandbox_mode = settings.exchange.sandbox_mode if sandbox is None else bool(sandbox)

    # Exchange sandbox/testnet is explicit. Local paper trading returns before
    # an exchange object is created, so market data is not silently moved to testnet.
    if sandbox_mode:
        if not config.get("has_sandbox", False):
            raise ValueError(f"{exchange_id} does not support CCXT sandbox/testnet mode")
        try:
            exchange.set_sandbox_mode(True)
        except Exception as e:
            raise ValueError(f"Sandbox mode unavailable for {exchange_id}: {e}") from e

    # Set default market type
    if "defaultType" in options:
        exchange.options["defaultType"] = options["defaultType"]

    return exchange


def _normalize_symbol(symbol: str) -> str:
    """Normalize symbol to exchange format."""
    if not symbol:
        return ""
    symbol = symbol.upper().replace(" ", "")
    for suffix in (".P", "PERP"):
        if symbol.endswith(suffix):
            symbol = symbol[:-len(suffix)]
            break
    if "/" in symbol:
        return symbol
    symbol = symbol.replace("-", "").replace("_", "").replace(":", "")
    # Add USDT suffix if missing and not already a pair
    if not symbol.endswith(("USDT", "USD", "BTC", "ETH", "BNB")):
        symbol = f"{symbol}USDT"
    return symbol


def _valid_stop_loss(direction: SignalDirection, entry: float, price: float | None) -> float | None:
    """Compatibility helper shared by legacy tests and callers."""
    try:
        value = float(price or 0)
        entry = float(entry or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0 or entry <= 0:
        return None
    # BUG FIX: Reject stop loss that equals entry price
    if value == entry:
        return None
    if direction == SignalDirection.LONG and value < entry:
        return value
    if direction == SignalDirection.SHORT and value > entry:
        return value
    return None


def _valid_take_profit(direction: SignalDirection, entry: float, price: float | None) -> float | None:
    """Compatibility helper shared by legacy tests and callers."""
    try:
        value = float(price or 0)
        entry = float(entry or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0 or entry <= 0:
        return None
    if direction == SignalDirection.LONG and value > entry:
        return value
    if direction == SignalDirection.SHORT and value < entry:
        return value
    return None


def _market_type_key(market_type: str | None) -> str:
    """Normalize exchange market type to spot vs contract."""
    value = str(market_type or "").lower().strip()
    if value == "spot":
        return "spot"
    if value in {"contract", "future", "futures", "swap", "linear", "inverse"}:
        return "contract"
    return ""


def _exchange_market_type(exchange: ccxt.Exchange, market_type: str | None = None) -> str:
    """Infer the desired market type from explicit config or exchange options."""
    explicit_type = _market_type_key(market_type)
    if explicit_type:
        return explicit_type
    options = getattr(exchange, "options", {}) or {}
    return _market_type_key(options.get("defaultType"))


def _market_matches_type(market: dict[str, Any], market_type: str) -> bool:
    """Check whether a CCXT market row matches the requested market family."""
    if not market_type:
        return True

    is_contract = bool(market.get("contract") or market.get("swap") or market.get("future"))
    if market_type == "contract":
        return is_contract
    if market_type == "spot":
        if market.get("spot") is True:
            return True
        return not is_contract
    return True


def _symbol_candidates(symbol: str, market_type: str | None = None) -> list[str]:
    """Return common CCXT symbol candidates for a TradingView-style ticker."""
    raw_symbol = str(symbol or "").upper().replace(" ", "")
    cleaned = _normalize_symbol(symbol).replace("/", "")
    quotes = ["USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB"]
    prefer_contract = _market_type_key(market_type) == "contract"
    candidates: list[str] = []
    if "/" in raw_symbol:
        candidates.append(raw_symbol)

    for quote in quotes:
        if cleaned.endswith(quote) and len(cleaned) > len(quote):
            base = cleaned[:-len(quote)]
            pair_symbol = f"{base}/{quote}"
            contract_symbol = f"{pair_symbol}:{quote}"
            if prefer_contract:
                candidates.extend([contract_symbol, pair_symbol, f"{base}{quote}"])
            else:
                candidates.extend([pair_symbol, contract_symbol, f"{base}{quote}"])
            break
    else:
        pair_symbol = f"{cleaned}/USDT"
        contract_symbol = f"{pair_symbol}:USDT"
        if prefer_contract:
            candidates.extend([contract_symbol, pair_symbol, f"{cleaned}USDT"])
        else:
            candidates.extend([pair_symbol, contract_symbol, f"{cleaned}USDT"])

    candidates.extend([cleaned, raw_symbol])

    # Preserve order while removing duplicates.
    return list(dict.fromkeys(candidates))


def _resolve_symbol(exchange: ccxt.Exchange, symbol: str, market_type: str | None = None) -> str:
    """Resolve a TradingView ticker into an exchange market symbol."""
    target_market_type = _exchange_market_type(exchange, market_type)
    candidates = _symbol_candidates(symbol, target_market_type)
    try:
        markets = exchange.load_markets()
    except Exception as e:
        logger.debug(f"[Exchange] Could not load markets for symbol resolution: {e}")
        return candidates[0]

    for candidate in candidates:
        market = markets.get(candidate)
        if isinstance(market, dict) and _market_matches_type(market, target_market_type):
            return candidate

    for candidate in candidates:
        if candidate in markets:
            return candidate

    cleaned = _normalize_symbol(symbol).replace("/", "")
    fallback_symbol = ""
    for market_symbol_raw, market in markets.items():
        market_symbol = str(market_symbol_raw)
        if not isinstance(market, dict):
            continue
        market_id = str(market.get("id", "")).upper().replace("-", "").replace("_", "").replace("/", "")
        compact_symbol = market_symbol.upper().replace("/", "").replace(":", "").replace("-", "").replace("_", "")
        if cleaned in {market_id, compact_symbol}:
            if _market_matches_type(market, target_market_type):
                return market_symbol
            if not fallback_symbol:
                fallback_symbol = market_symbol

    if fallback_symbol:
        return fallback_symbol

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
    sandbox_mode = bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode))

    if not live_trading:
        logger.warning("[Exchange] 🔶 PAPER TRADING MODE - not sending real orders")
        return _simulate_order(decision)

    if not _CCXT_AVAILABLE:
        return {
            "status": "error",
            "reason": "ccxt is not installed; install project requirements to enable live exchange execution",
        }

    if sandbox_mode:
        logger.warning("[Exchange] 🧪 EXCHANGE SANDBOX MODE - sending orders to testnet/sandbox")

    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=live_trading,
        sandbox=sandbox_mode,
        market_type=exchange_config.get("market_type") or settings.exchange.market_type,
    )
    symbol = await asyncio.to_thread(
        _resolve_symbol,
        exchange,
        decision.ticker,
        exchange_config.get("market_type") or settings.exchange.market_type,
    )

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

        # Support both market and limit orders
        order_type = str(getattr(decision, "order_type", "") or "").strip().lower()
        if not order_type or order_type not in ("market", "limit"):
            order_type = "market"

        if order_type == "limit" and decision.entry_price and decision.entry_price > 0:
            logger.info(f"[Exchange] Placing {side} LIMIT order: {symbol} qty={decision.quantity} @ {decision.entry_price}")
            order = await _create_exchange_order(
                exchange,
                symbol=symbol,
                order_type="limit",
                side=side,
                amount=decision.quantity,
                price=decision.entry_price,
            )
        else:
            logger.info(f"[Exchange] Placing {side} MARKET order: {symbol} qty={decision.quantity}")
            order = await _create_exchange_order(
                exchange,
                symbol=symbol,
                order_type="market",
                side=side,
                amount=decision.quantity,
            )

        order_id = order.get("id", "unknown")
        order_status = order.get("status", "unknown")
        logger.info(f"[Exchange] Entry order placed: {order_id} (status={order_status})")

        result_status = "pending" if order_type == "limit" and order_status in {"open", "new"} else "filled"
        result = {
            "status": result_status,
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": decision.quantity,
            "entry_price": order.get("average") or order.get("price") or decision.entry_price,
            "sandbox_mode": sandbox_mode,
            "order_type": order_type,
            "exchange_order_status": order_status,
        }
        if leverage:
            result["recommended_leverage"] = leverage

        if decision.trailing_stop:
            result["trailing_stop_config"] = {
                "mode": decision.trailing_stop.mode.value,
                "trail_pct": decision.trailing_stop.trail_pct,
                "activation_profit_pct": decision.trailing_stop.activation_profit_pct,
                "trailing_step_pct": decision.trailing_stop.trailing_step_pct,
            }

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
                ts_order = await _create_exchange_order(
                    exchange,
                    symbol=symbol,
                    order_type="trailing_stop_market",
                    side=sl_side,
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
        return {"status": "error", "reason": f"Insufficient funds: {e}"}
    except ccxt.NetworkError as e:
        logger.error(f"[Exchange] Network error: {e}")
        return {"status": "error", "reason": f"Network error: {e}"}
    except Exception as e:
        logger.error(f"[Exchange] Order failed: {e}")
        return {"status": "error", "reason": f"Order execution failed: {e}"}


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


def _conditional_order_attempts(exchange_id: str, kind: str, trigger_price: float) -> list[tuple[str, dict[str, Any]]]:
    """Return exchange-aware conditional-order candidates."""
    reduce_params: dict[str, Any] = {"reduceOnly": True, "closePosition": False}
    if kind == "take_profit":
        candidates: list[tuple[str, dict[str, Any]]] = [
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
    exchange_id = _exchange_id(exchange)
    errors = []
    for order_type, params in _conditional_order_attempts(exchange_id, kind, trigger_price):
        try:
            return await _create_exchange_order(
                exchange,
                symbol=symbol,
                order_type=order_type,
                side=side,
                amount=amount,
                params=params,
            )
        except Exception as exc:
            errors.append(f"{order_type}: {exc}")
            logger.debug(f"[Exchange] {exchange_id} {kind} candidate failed: {order_type} {exc}")
    raise RuntimeError("; ".join(errors[-3:]) or f"Failed to create {kind} order")


async def _cancel_exchange_order(exchange, symbol: str, order_id: str) -> dict:
    """Cancel an exchange order by id while tolerating already-gone orders."""
    if not order_id:
        return {"status": "skipped", "order_id": "", "symbol": symbol}

    try:
        result = await asyncio.to_thread(exchange.cancel_order, order_id, symbol)
        return {
            "status": "cancelled",
            "order_id": str((result or {}).get("id") or order_id),
            "symbol": symbol,
        }
    except Exception as exc:
        if _is_order_not_found_error(exc):
            return {"status": "not_found", "order_id": order_id, "symbol": symbol}
        logger.error(f"[Exchange] Failed to cancel order {order_id} on {symbol}: {exc}")
        return {"status": "error", "order_id": order_id, "symbol": symbol, "reason": str(exc)}


async def cancel_order(order_id: str, ticker: str, exchange_config: dict | None = None) -> dict:
    """Cancel a specific exchange order."""
    exchange_config = exchange_config or {}
    if not order_id:
        return {"status": "skipped", "order_id": "", "ticker": ticker}
    if not exchange_config.get("live_trading", settings.exchange.live_trading):
        return {"status": "simulated", "order_id": order_id, "ticker": ticker}

    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=True,
        sandbox=bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode)),
        market_type=exchange_config.get("market_type") or settings.exchange.market_type,
    )
    try:
        symbol = await asyncio.to_thread(
            _resolve_symbol,
            exchange,
            ticker,
            exchange_config.get("market_type") or settings.exchange.market_type,
        )
        return await _cancel_exchange_order(exchange, symbol, order_id)
    except Exception as exc:
        logger.error(f"[Exchange] Failed to cancel order {order_id} for {ticker}: {exc}")
        return {"status": "error", "order_id": order_id, "ticker": ticker, "reason": str(exc)}


async def place_protective_stop(
    ticker: str,
    direction: str,
    quantity: float,
    stop_price: float,
    exchange_config: dict | None = None,
    existing_order_id: str | None = None,
) -> dict:
    """Place a reduce-only protective stop for an already-open monitored position."""
    exchange_config = exchange_config or {}
    if not exchange_config.get("live_trading", settings.exchange.live_trading):
        return {"status": "simulated", "stop_price": stop_price}
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=True,
        sandbox=bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode)),
        market_type=exchange_config.get("market_type") or settings.exchange.market_type,
    )
    try:
        symbol = await asyncio.to_thread(
            _resolve_symbol,
            exchange,
            ticker,
            exchange_config.get("market_type") or settings.exchange.market_type,
        )
        side = "sell" if str(direction).lower() == SignalDirection.LONG.value else "buy"
        cancelled_order_id = ""
        if existing_order_id:
            cancel_result = await _cancel_exchange_order(exchange, symbol, str(existing_order_id))
            if cancel_result.get("status") == "error":
                return {
                    "status": "error",
                    "reason": cancel_result.get("reason") or "Failed to replace protective stop",
                    "symbol": symbol,
                    "stop_price": stop_price,
                }
            cancelled_order_id = str(cancel_result.get("order_id") or existing_order_id)
        order = await _create_conditional_order(exchange, symbol, "stop_loss", side, quantity, stop_price)
        result = {"status": "placed", "order_id": order.get("id"), "symbol": symbol, "stop_price": stop_price}
        if cancelled_order_id:
            result["replaced_order_id"] = cancelled_order_id
        return result
    except Exception as e:
        logger.error(f"[Exchange] Failed to place protective stop: {e}")
        return {"status": "error", "reason": str(e)}


async def _close_position(exchange: ccxt.Exchange, symbol: str, side: str) -> dict:
    """Close an existing position."""
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions, [symbol])
        for pos in positions:
            if pos["symbol"] == symbol and float(pos.get("contracts", 0)) > 0:
                amount = float(pos["contracts"])
                order = await _create_exchange_order(
                    exchange,
                    symbol=symbol,
                    order_type="market",
                    side=side,
                    amount=amount,
                    params={"reduceOnly": True},
                )
                logger.info(f"[Exchange] ✅ Position closed: {order.get('id')}")
                exit_price = order.get("average") or order.get("price") or pos.get("markPrice") or pos.get("entryPrice")
                return {"status": "closed", "order_id": order.get("id"), "exit_price": exit_price}
        return {"status": "no_position", "reason": "No open position to close"}
    except Exception as e:
        logger.error(f"[Exchange] Failed to close position: {e}")
        return {"status": "error", "reason": "Failed to close position"}


def _simulate_order(decision: TradeDecision) -> dict:
    """Simulate order execution for paper trading with intelligent entry tracking."""
    tp_info = []
    for i, tp in enumerate(decision.take_profit_levels):
        tp_info.append({
            "level": i + 1,
            "price": tp.price,
            "qty_pct": tp.qty_pct,
            "status": "simulated",
        })

    trailing_mode = decision.trailing_stop.mode if decision.trailing_stop else TrailingStopMode.NONE
    order_type = str(getattr(decision, "order_type", "") or "").strip().lower()
    if not order_type or order_type not in ("market", "limit"):
        order_type = "market"

    trailing_config = {}
    if decision.trailing_stop:
        trailing_config = {
            "mode": trailing_mode.value if hasattr(trailing_mode, "value") else str(trailing_mode),
            "trail_pct": decision.trailing_stop.trail_pct,
            "activation_profit_pct": decision.trailing_stop.activation_profit_pct,
            "trailing_step_pct": decision.trailing_stop.trailing_step_pct,
        }

    if order_type == "limit" and decision.entry_price and decision.entry_price > 0:
        status = "pending"
        note = f"Limit order pending at {decision.entry_price}. Waiting for price to reach entry."
        logger.info(
            f"[Exchange] 📝 SIMULATED LIMIT ORDER: {decision.direction} {decision.ticker} "
            f"qty={decision.quantity} entry={decision.entry_price} "
            f"(waiting for price to reach entry point)"
        )
    else:
        status = "simulated"
        note = "Market order - immediate execution at current price"
        logger.info(
            f"[Exchange] ✅ SIMULATED MARKET ORDER: {decision.direction} {decision.ticker} "
            f"qty={decision.quantity} entry={decision.entry_price} SL={decision.stop_loss} TPs={len(decision.take_profit_levels)} "
        )

    return {
        "status": status,
        "symbol": decision.ticker,
        "direction": decision.direction.value if decision.direction else "unknown",
        "quantity": decision.quantity,
        "entry_price": decision.entry_price,
        "stop_loss": decision.stop_loss,
        "take_profit": decision.take_profit,
        "take_profit_orders": tp_info,
        "trailing_stop_config": trailing_config,
        "trailing_stop_mode": trailing_mode if isinstance(trailing_mode, str) else trailing_mode.value,
        "trailing_pct": decision.trailing_stop.trail_pct if decision.trailing_stop else 0,
        "sandbox_mode": False,
        "order_type": order_type,
        "limit_timeout_secs": decision.limit_timeout_secs,
        "note": note,
    }


async def get_account_balance(exchange_config: dict | None = None) -> dict:
    """Fetch account balance from exchange."""
    exchange_config = exchange_config or {}
    if not bool(exchange_config.get("live_trading", settings.exchange.live_trading)):
        return {
            "mode": "paper",
            "quote": "USDT",
            "total_quote": settings.risk.account_equity_usdt,
            "free_quote": settings.risk.account_equity_usdt,
            "used_quote": 0.0,
            "total": {"USDT": settings.risk.account_equity_usdt},
            "free": {"USDT": settings.risk.account_equity_usdt},
            "used": {"USDT": 0.0},
        }
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=True,
        sandbox=bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode)),
        market_type=exchange_config.get("market_type") or settings.exchange.market_type,
    )
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
        quote = "USDT" if "USDT" in balance.get("total", {}) else "USD"
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


async def get_balance(exchange_config: dict | None = None) -> dict:
    """Fetch account balance from exchange."""
    exchange_config = exchange_config or {}
    if not bool(exchange_config.get("live_trading", settings.exchange.live_trading)):
        return {
            "mode": "paper",
            "total": {"USDT": settings.risk.account_equity_usdt},
            "free": {"USDT": settings.risk.account_equity_usdt},
            "used": {"USDT": 0.0},
        }
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=True,
        sandbox=bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode)),
        market_type=exchange_config.get("market_type") or settings.exchange.market_type,
    )
    try:
        balance = await asyncio.to_thread(exchange.fetch_balance)
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


async def get_ticker(symbol: str, exchange_config: dict | None = None) -> dict:
    """Fetch ticker data for a symbol."""
    exchange_config = exchange_config or {}
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=bool(exchange_config.get("live_trading", settings.exchange.live_trading)),
        sandbox=bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode)),
        market_type=exchange_config.get("market_type") or settings.exchange.market_type,
    )
    try:
        resolved_symbol = await asyncio.to_thread(
            _resolve_symbol,
            exchange,
            symbol,
            exchange_config.get("market_type") or settings.exchange.market_type,
        )
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


async def get_latest_candle(symbol: str, timeframe: str = "1m", exchange_config: dict | None = None) -> dict:
    """Fetch the latest OHLCV candle for paper-trading TP/SL checks."""
    exchange_config = exchange_config or {}
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=bool(exchange_config.get("live_trading", settings.exchange.live_trading)),
        sandbox=bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode)),
        market_type=exchange_config.get("market_type") or settings.exchange.market_type,
    )
    try:
        resolved_symbol = await asyncio.to_thread(
            _resolve_symbol,
            exchange,
            symbol,
            exchange_config.get("market_type") or settings.exchange.market_type,
        )
        candles = await asyncio.to_thread(exchange.fetch_ohlcv, resolved_symbol, timeframe, None, 2)
        if not candles:
            ticker = await asyncio.to_thread(exchange.fetch_ticker, resolved_symbol)
            last = ticker.get("last") or ticker.get("close")
            return {"symbol": resolved_symbol, "open": last, "high": last, "low": last, "close": last}
        ts, open_, high, low, close, volume = candles[-1]
        return {
            "symbol": resolved_symbol,
            "timestamp": ts,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
        }
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch latest candle for {symbol}: {e}")
        return {}


async def get_open_positions(exchange_config: dict | None = None) -> list[dict]:
    """Fetch open positions from exchange."""
    exchange_config = exchange_config or {}
    if not bool(exchange_config.get("live_trading", settings.exchange.live_trading)):
        return []
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=True,
        sandbox=bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode)),
        market_type=exchange_config.get("market_type") or settings.exchange.market_type,
    )
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
                entry_price = pos.get('entryPrice')
                mark_price = pos.get('markPrice')

                # BUG FIX: Always calculate percentage from entry vs mark price
                # Don't trust exchange's 'percentage' field as it may contain incorrect data
                percentage = None
                if entry_price is not None and mark_price is not None:
                    try:
                        entry = float(entry_price)
                        mark = float(mark_price)
                        if entry > 0:
                            side = str(pos.get('side') or '').lower()
                            if side == 'long':
                                percentage = ((mark - entry) / entry) * 100
                            elif side == 'short':
                                percentage = ((entry - mark) / entry) * 100
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass

                # Fallback: calculate from unrealized_pnl / notional if available
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


async def get_recent_orders(symbol: str | None = None, limit: int = 50, exchange_config: dict | None = None) -> list[dict]:
    """Fetch recent closed orders from exchange."""
    exchange_config = exchange_config or {}
    if not bool(exchange_config.get("live_trading", settings.exchange.live_trading)):
        return []
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=True,
        sandbox=bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode)),
        market_type=exchange_config.get("market_type") or settings.exchange.market_type,
    )
    try:
        if symbol:
            resolved_symbol = await asyncio.to_thread(
                _resolve_symbol,
                exchange,
                symbol,
                exchange_config.get("market_type") or settings.exchange.market_type,
            )
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
                "average": o.get("average"),
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


async def test_exchange_connection(
    exchange_id: str,
    api_key: str,
    api_secret: str,
    password: str = "",
    sandbox_mode: bool = False,
    market_type: str | None = None,
) -> dict:
    """Test if exchange API keys are valid."""
    try:
        exchange = _get_or_create_exchange(
            exchange_id=exchange_id,
            api_key=api_key,
            api_secret=api_secret,
            password=password,
            live=True,
            sandbox=sandbox_mode,
            market_type=market_type or settings.exchange.market_type,
        )
        await asyncio.to_thread(exchange.fetch_balance)
        mode = " sandbox/testnet" if sandbox_mode else ""
        return {"success": True, "message": f"Connected to {exchange_id}{mode} successfully"}
    except ccxt.AuthenticationError as e:
        return {"success": False, "message": f"Authentication failed: {e}"}
    except Exception as e:
        return {"success": False, "message": f"Connection failed: {e}"}
