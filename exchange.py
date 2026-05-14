"""
Signal Server - Multi-Exchange Executor
Supports: Binance, OKX, Bybit, Bitget, Gate.io, Coinbase
Enhanced with multi-TP and trailing-stop execution
P0-FIX: Leverage setup retry mechanism for reliability
"""
import asyncio
import hashlib as _hashlib
import inspect
import math
import threading as _threading
import time
from typing import Any

from loguru import logger

from core.config import settings
from core.utils.common import safe_float as _safe_float_common
from models import SignalDirection, TradeDecision, TrailingStopMode

safe_float = _safe_float_common

# P0-FIX: Leverage retry configuration
_LEVERAGE_MAX_RETRIES = 3
_LEVERAGE_RETRY_DELAY_BASE = 1.0  # seconds, exponential backoff
_LEVERAGE_RETRYABLE_ERRORS = ["NetworkError", "Timeout", "ExchangeNotAvailable", "DDoSProtection"]
_OKX_LEVERAGE_ERROR_CODES = ["11045", "51000", "51020"]
_MARKET_MAX_LEVERAGE_CACHE: dict[str, float] = {}  # key: "exchange_id:symbol" -> max_leverage

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


def _is_okx_leverage_error(error_msg: str) -> tuple[bool, str]:
    """Check if error is an OKX leverage-related error (11045, 51000, 51020)."""
    text = str(error_msg).lower()
    for code in _OKX_LEVERAGE_ERROR_CODES:
        if f'"{code}"' in text or f"'{code}'" in text or f"code {code}" in text:
            return True, code
    if '"code":"11045"' in text or '"11045"' in text:
        return True, "11045"
    return False, ""


async def _set_leverage_with_retry(exchange, leverage: int, symbol: str, max_retries: int = _LEVERAGE_MAX_RETRIES) -> dict:
    """P0-FIX: Set leverage with exponential backoff retry mechanism.

    Args:
        exchange: CCXT exchange instance
        leverage: Target leverage (e.g., 10 for 10x)
        symbol: Trading symbol (e.g., "BTC/USDT:USDT")
        max_retries: Maximum retry attempts (default: 3)

    Returns:
        dict with "success": True/False and optional "error" message

    Retry Strategy:
        - Retries on transient errors (NetworkError, Timeout, DDoSProtection)
        - Exponential backoff: 1s, 2s, 4s
        - For OKX leverage errors (11045), tries switching margin mode (cross <-> isolated)
        - Does NOT retry on authentication errors or permanent exchange errors
        - Logs all attempts for observability
    """
    if leverage <= 1:
        logger.debug(f"[P0-FIX] Leverage {leverage}x <= 1x, skip setup for {symbol}")
        return {"success": True}

    exchange_id = str(getattr(exchange, "id", "") or "").lower().strip()
    margin_modes_to_try = ["cross"]
    if exchange_id == "okx":
        margin_modes_to_try = ["cross", "isolated"]

    for margin_mode in margin_modes_to_try:
        for attempt in range(max_retries):
            try:
                if exchange_id == "okx":
                    params = {"tdMode": margin_mode}
                    await asyncio.to_thread(exchange.set_leverage, leverage, symbol, params)
                else:
                    await asyncio.to_thread(exchange.set_leverage, leverage, symbol)
                logger.info(f"[P0-FIX] Leverage set successfully: {symbol} {leverage}x (mode={margin_mode}, attempt {attempt + 1}/{max_retries})")
                return {"success": True}

            except ccxt.AuthenticationError as e:
                logger.error(f"[P0-FIX] Authentication error setting leverage for {symbol}: {e}")
                return {"success": False, "error": f"Authentication failed: {e}", "abort": True}

            except ccxt.ExchangeError as e:
                error_name = type(e).__name__
                error_msg = str(e)

                is_okx_lev, okx_code = _is_okx_leverage_error(error_msg)
                if is_okx_lev and exchange_id == "okx" and margin_mode == "cross" and len(margin_modes_to_try) > 1:
                    logger.warning(f"[P0-FIX] OKX leverage error {okx_code} with cross mode, trying isolated mode for {symbol}")
                    break

                is_retryable = any(
                    retryable_err.lower() in error_msg.lower() or retryable_err in error_name
                    for retryable_err in _LEVERAGE_RETRYABLE_ERRORS
                ) or is_okx_lev

                if is_retryable and attempt < max_retries - 1:
                    delay = _LEVERAGE_RETRY_DELAY_BASE * (2 ** attempt)
                    logger.warning(
                        f"[P0-FIX] Retrying leverage setup for {symbol} {leverage}x "
                        f"(attempt {attempt + 1}/{max_retries}) after {error_name}: {error_msg}. "
                        f"Retry in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    if is_okx_lev and margin_mode == "cross":
                        break
                    logger.error(
                        f"[P0-FIX] Failed to set leverage {leverage}x for {symbol} after {attempt + 1} attempts: {error_name}: {error_msg}"
                    )
                    return {"success": False, "error": f"Exchange error: {error_msg}", "abort": leverage > 1}

            except Exception as e:
                error_name = type(e).__name__
                error_msg = str(e)

                is_okx_lev, okx_code = _is_okx_leverage_error(error_msg)
                if is_okx_lev and exchange_id == "okx" and margin_mode == "cross" and len(margin_modes_to_try) > 1:
                    logger.warning(f"[P0-FIX] OKX leverage error {okx_code} with cross mode, trying isolated mode for {symbol}")
                    break

                if attempt < max_retries - 1:
                    delay = _LEVERAGE_RETRY_DELAY_BASE * (2 ** attempt)
                    logger.warning(
                        f"[P0-FIX] Unexpected error setting leverage for {symbol}, retrying "
                        f"(attempt {attempt + 1}/{max_retries}): {error_name}: {error_msg}. "
                        f"Retry in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                else:
                    if is_okx_lev and margin_mode == "cross":
                        break
                    logger.error(
                        f"[P0-FIX] Failed to set leverage {leverage}x for {symbol} after {max_retries} attempts: {error_name}: {error_msg}"
                    )
                    is_transient = isinstance(e, (ccxt.NetworkError, ccxt.RequestTimeout, ccxt.DDoSProtection))
                    return {"success": False, "error": f"Unexpected error: {error_msg}", "abort": leverage > 1 and not is_transient}

    return {"success": False, "error": "Max retries exceeded without success", "abort": True}


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


def _order_create_attempts(exchange, side: str, params: dict[str, Any] | None = None, position_side: str | None = None) -> list[dict[str, Any]]:
    base = dict(params or {})
    exchange_id = _exchange_id(exchange)

    if exchange_id == "bybit" and position_side:
        pos_idx = "1" if position_side.lower() == "long" else "2"
        base["positionIdx"] = pos_idx
        return [base]

    if exchange_id != "okx":
        return [base]

    # Read margin mode from exchange options (defaults to "cross" if not set)
    exchange_options = getattr(exchange, "options", {}) or {}
    margin_mode = str(exchange_options.get("defaultMarginMode") or "cross").lower().strip()

    # For OKX hedge mode, posSide should match the POSITION being operated on
    # - Opening LONG: side=buy, position_side=long (or derived from side)
    # - Opening SHORT: side=sell, position_side=short
    # - Closing LONG: side=sell, position_side=long (NOT short!)
    # - Closing SHORT: side=buy, position_side=short (NOT long!)
    # - TP/SL for LONG: side=sell, position_side=long
    # - TP/SL for SHORT: side=buy, position_side=short

    # If position_side is explicitly provided (close/TP/SL), use it
    # Otherwise derive from order side (open orders)
    if position_side:
        pos_side = position_side.lower()
    else:
        pos_side = _okx_position_side(side)

    return [
        {**base, "tdMode": base.get("tdMode") or margin_mode},
        {**base, "tdMode": base.get("tdMode") or margin_mode, "posSide": pos_side},
    ]


async def _create_exchange_order(
    exchange,
    symbol: str,
    order_type: str,
    side: str,
    amount: float,
    price: float | None = None,
    params: dict[str, Any] | None = None,
    position_side: str | None = None,
    allow_amount_increase: bool = True,
) -> dict:
    """Create an exchange order with small exchange-specific retries.

    Includes market precision and limits validation.

    Args:
        position_side: For OKX hedge mode, the actual position side ('long' or 'short').
                       Required for close/TP/SL orders to target correct position.
                       Optional for open orders (derived from side).
    """
    import time

    from core.metrics import EXCHANGE_ERRORS, record_exchange_request

    # Validate amount against market limits before placing order
    requested_amount = amount
    amount = _validate_and_adjust_amount(exchange, symbol, amount)
    if not allow_amount_increase and amount > requested_amount:
        raise ValueError(
            f"Adjusted close amount {amount} exceeds requested rollback amount {requested_amount}"
        )

    exchange_id = _exchange_id(exchange)
    errors: list[str] = []
    for attempt_params in _order_create_attempts(exchange, side, params, position_side):
        start = time.time()
        try:
            if price is None:
                result = await asyncio.to_thread(
                    exchange.create_order,
                    symbol=symbol,
                    type=order_type,
                    side=side,
                    amount=amount,
                    params=attempt_params,
                )
            else:
                result = await asyncio.to_thread(
                    exchange.create_order,
                    symbol=symbol,
                    type=order_type,
                    side=side,
                    amount=amount,
                    price=price,
                    params=attempt_params,
                )
            record_exchange_request(
                exchange=exchange_id,
                endpoint="create_order",
                status="success",
                latency=time.time() - start,
            )
            return result
        except ccxt.BaseError as exc:
            latency = time.time() - start
            record_exchange_request(
                exchange=exchange_id,
                endpoint="create_order",
                status="error",
                latency=latency,
            )
            EXCHANGE_ERRORS.labels(exchange=exchange_id, error_type=type(exc).__name__).inc()
            errors.append(f"{attempt_params}: {exc}")
            if not (exchange_id == "okx" and _is_okx_pos_side_error(exc)):
                break
        except Exception as exc:
            latency = time.time() - start
            record_exchange_request(
                exchange=exchange_id,
                endpoint="create_order",
                status="error",
                latency=latency,
            )
            EXCHANGE_ERRORS.labels(exchange=exchange_id, error_type=type(exc).__name__).inc()
            errors.append(f"{attempt_params}: {exc}")
            if not (exchange_id == "okx" and _is_okx_pos_side_error(exc)):
                break
    raise RuntimeError("; ".join(errors[-2:]) or f"Failed to create {order_type} order")


def _validate_and_adjust_amount(exchange, symbol: str, amount: float) -> float:
    """
    Validate and adjust order amount against exchange market limits.

    Handles:
    - Minimum order amount (e.g., XAU requires min 1 unit)
    - Maximum order amount (e.g., SHIB has max limit per order)
    - Amount precision (e.g., some markets require integer amounts)

    Returns adjusted amount that meets exchange requirements.
    """
    if amount <= 0:
        return amount

    try:
        markets = exchange.load_markets()
        market = markets.get(symbol)
        if not isinstance(market, dict):
            logger.warning(f"[Exchange] Market {symbol} not found, using original amount")
            return amount

        limits = market.get("limits", {})
        precision = market.get("precision", {})

        # Get limits
        min_amount = float(limits.get("amount", {}).get("min", 0) or 0)
        max_amount = float(limits.get("amount", {}).get("max", float("inf")) or float("inf"))

        # Get precision
        amount_precision = precision.get("amount")
        if amount_precision is None:
            amount_precision = 0
        elif isinstance(amount_precision, int):
            amount_precision = amount_precision
        elif isinstance(amount_precision, float) and amount_precision > 0:
            amount_precision = -int(round(math.log10(amount_precision)))

        # Adjust for minimum amount
        if min_amount > 0 and amount < min_amount:
            logger.warning(
                f"[Exchange] Amount {amount} < min_amount {min_amount} for {symbol}, "
                f"adjusting to minimum"
            )
            amount = min_amount

        # Adjust for maximum amount
        if max_amount < float("inf") and amount > max_amount:
            logger.warning(
                f"[Exchange] Amount {amount} > max_amount {max_amount} for {symbol}, "
                f"adjusting to maximum"
            )
            amount = max_amount

        # Adjust for precision (round to valid precision)
        if amount_precision >= 0:
            amount = round(amount, amount_precision)
        else:
            step = 10 ** amount_precision
            amount = round(amount / step) * step

        # Additional check: OKX specific - some markets require integer amounts
        exchange_id = _exchange_id(exchange)
        if exchange_id == "okx":
            if "XAU" in symbol.upper() or "GOLD" in symbol.upper():
                amount = max(1, int(round(amount)))
                logger.info(f"[Exchange] OKX Gold/XAU: adjusted amount to integer {amount}")

        if amount <= 0:
            logger.error(f"[Exchange] Adjusted amount is 0 for {symbol}")
            return min_amount if min_amount > 0 else 1

        logger.debug(f"[Exchange] Amount validation: {symbol} adjusted={amount}, min={min_amount}, max={max_amount}")
        return amount

    except Exception as e:
        logger.warning(f"[Exchange] Could not validate amount for {symbol}: {e}")
        return amount


def get_market_limits(exchange_id: str, symbol: str, market_type: str = "contract") -> dict:
    """
    Get market limits for a symbol without creating full exchange instance.

    Returns dict with:
    - min_amount: Minimum order quantity
    - max_amount: Maximum order quantity
    - min_cost: Minimum order value (USDT)
    - max_cost: Maximum order value (USDT)
    - amount_precision: Decimal places for quantity
    - price_precision: Decimal places for price

    This is used during position size calculation to respect exchange limits.
    """
    if not _CCXT_AVAILABLE:
        return {}

    try:
        # Create temporary exchange instance just to fetch markets
        exchange = _get_or_create_exchange(
            exchange_id=exchange_id,
            api_key=None,
            api_secret=None,
            password="",
            live=False,
            sandbox=False,
            market_type=market_type,
        )

        markets = exchange.load_markets()

        candidates = _symbol_candidates(symbol, market_type)
        market = None
        resolved_symbol = None
        for candidate in candidates:
            market = markets.get(candidate)
            if isinstance(market, dict):
                resolved_symbol = candidate
                break

        if not isinstance(market, dict):
            logger.warning(f"[Exchange] Market {symbol} not found in {exchange_id} (tried: {candidates})")
            return {}

        limits = market.get("limits", {})
        precision = market.get("precision", {})

        # Parse limits
        min_amount = float(limits.get("amount", {}).get("min", 0) or 0)
        max_amount = float(limits.get("amount", {}).get("max", float("inf")) or float("inf"))
        min_cost = float(limits.get("cost", {}).get("min", 0) or 0)
        max_cost = float(limits.get("cost", {}).get("max", float("inf")) or float("inf"))

        # Parse precision
        amount_precision_raw = precision.get("amount")
        price_precision_raw = precision.get("price")

        amount_precision = 0
        if amount_precision_raw is not None:
            if isinstance(amount_precision_raw, int):
                amount_precision = amount_precision_raw
            elif isinstance(amount_precision_raw, float) and amount_precision_raw > 0:
                amount_precision = -int(round(math.log10(amount_precision_raw)))

        price_precision = 0
        if price_precision_raw is not None:
            if isinstance(price_precision_raw, int):
                price_precision = price_precision_raw
            elif isinstance(price_precision_raw, float) and price_precision_raw > 0:
                price_precision = -int(round(math.log10(price_precision_raw)))

        # OKX specific: XAU/GOLD requires integer amounts
        if exchange_id.lower() == "okx":
            if "XAU" in symbol.upper() or "GOLD" in symbol.upper():
                min_amount = max(1, int(min_amount) if min_amount > 0 else 1)
                amount_precision = 0  # Integer only

        contract_size = 1.0
        if market.get("contractSize"):
            try:
                contract_size = float(market.get("contractSize") or 1.0)
            except (TypeError, ValueError):
                contract_size = 1.0

        result = {
            "min_amount": min_amount,
            "max_amount": max_amount,
            "min_cost": min_cost,
            "max_cost": max_cost,
            "amount_precision": amount_precision,
            "price_precision": price_precision,
            "contract_size": contract_size,
            "symbol": resolved_symbol or symbol,
            "original_symbol": symbol,
            "exchange": exchange_id,
        }

        if contract_size > 1.0:
            logger.debug(
                f"[Exchange] Market limits for {resolved_symbol or symbol}: "
                f"min_amount={min_amount}, max_amount={max_amount}, "
                f"min_cost={min_cost}, max_cost={max_cost}, "
                f"contractSize={contract_size}"
            )
        else:
            logger.debug(
                f"[Exchange] Market limits for {resolved_symbol or symbol}: "
                f"min_amount={min_amount}, max_amount={max_amount}, "
                f"min_cost={min_cost}, max_cost={max_cost}"
            )
        return result

    except Exception as e:
        logger.warning(f"[Exchange] Could not get market limits for {symbol} on {exchange_id}: {e}")
        return {}


def adjust_quantity_for_limits(
    quantity: float,
    price: float,
    limits: dict,
) -> float:
    """
    Adjust quantity to respect exchange market limits.

    Args:
        quantity: Original calculated quantity
        price: Entry price
        limits: Market limits dict from get_market_limits()

    Returns:
        Adjusted quantity that meets all exchange requirements
    """
    if quantity <= 0 or price <= 0 or not limits:
        return quantity

    min_amount = limits.get("min_amount", 0)
    max_amount = limits.get("max_amount", float("inf"))
    min_cost = limits.get("min_cost", 0)
    max_cost = limits.get("max_cost", float("inf"))
    amount_precision = limits.get("amount_precision", 0)
    contract_size = limits.get("contract_size", 1.0)

    # Calculate order value (cost) - for contract markets, cost = quantity * price * contractSize
    current_cost = quantity * price * contract_size

    adjustments = []

    if price <= 0.0001:
        logger.warning(f"[Exchange] Invalid price {price}, skipping cost-based adjustments")
        return quantity

    # Check minimum cost (order value)
    if min_cost > 0 and current_cost < min_cost:
        min_qty_for_cost = min_cost / (price * contract_size)
        if min_qty_for_cost > quantity:
            quantity = min_qty_for_cost
            adjustments.append(f"cost_min: increased to {min_cost} USDT")

    # Check maximum cost (order value)
    if max_cost < float("inf") and current_cost > max_cost:
        max_qty_for_cost = max_cost / (price * contract_size)
        if max_qty_for_cost < quantity:
            quantity = max_qty_for_cost
            adjustments.append(f"cost_max: reduced to {max_cost} USDT")

    # Check minimum amount (quantity)
    if min_amount > 0 and quantity < min_amount:
        quantity = min_amount
        adjustments.append(f"amount_min: increased to {min_amount}")

    # Check maximum amount (quantity)
    if max_amount < float("inf") and quantity > max_amount:
        quantity = max_amount
        adjustments.append(f"amount_max: reduced to {max_amount}")

    # Apply precision
    if amount_precision >= 0:
        quantity = round(quantity, amount_precision)
    else:
        step = 10 ** amount_precision
        quantity = round(quantity / step) * step

    # OKX Gold/XAU: force integer
    if "XAU" in limits.get("symbol", "").upper() or "GOLD" in limits.get("symbol", "").upper():
        quantity = max(1, int(round(quantity)))

    # Final check
    if quantity <= 0:
        logger.error("[Exchange] Adjusted quantity is 0, falling back to minimum")
        return max(min_amount, min_cost / price) if min_amount > 0 or min_cost > 0 else 1

    if adjustments:
        logger.info(
            f"[Exchange] Quantity adjusted for limits: "
            f"original={quantity}, final={quantity}, adjustments: {', '.join(adjustments)}"
        )

    return quantity


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
    margin_mode: str | None = None,
) -> ccxt.Exchange:
    """Return a cached CCXT instance or create a new one.

    Uses double-checked locking pattern to avoid race conditions
    while minimizing lock contention.

    Includes health check to evict stale/unhealthy connections.
    """
    eid = (exchange_id or settings.exchange.name).lower().strip()
    # SECURITY: Hash credentials individually to avoid plaintext concatenation in memory
    key_parts = []
    for part in [api_key, api_secret, password]:
        h = _hashlib.sha256()
        h.update(str(part or "").encode())
        key_parts.append(h.hexdigest())
    cred_hash = _hashlib.sha256(":".join(key_parts).encode()).hexdigest()
    sb = settings.exchange.sandbox_mode if sandbox is None else bool(sandbox)
    market_key = str(market_type or settings.exchange.market_type or "contract").lower().strip()
    margin_key = str(margin_mode or settings.risk.margin_mode or "cross").lower().strip()
    cache_key = f"{eid}:{sb}:{market_key}:{margin_key}:{cred_hash}"

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
            except Exception as e:
                logger.debug(f"[Exchange] Error closing evicted cached instance: {e}")
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
                    except Exception as e:
                        logger.debug(f"[Exchange] Error closing unhealthy cached instance: {e}")
                else:
                    try:
                        close = getattr(existing, "close", None)
                        if close:
                            close()
                    except Exception as e:
                        logger.debug(f"[Exchange] Error closing failed health check instance: {e}")
                    _exchange_pool.pop(cache_key, None)
                    _exchange_pool_health.pop(cache_key, None)
                    logger.info(f"[Exchange] Health check failed, rebuilding instance for {cache_key}")
                    return _get_or_create_exchange(
                        exchange_id=exchange_id,
                        api_key=api_key,
                        api_secret=api_secret,
                        password=password,
                        live=live,
                        sandbox=sandbox,
                        market_type=market_type,
                        margin_mode=margin_mode,
                    )
        else:
            return existing

    with _exchange_pool_lock:
        existing = _exchange_pool.get(cache_key)
        if existing is not None:
            return existing

        instance = _build_exchange(exchange_id, api_key, api_secret, password, live, sandbox, market_type, margin_mode)

        if len(_exchange_pool) >= settings.exchange.pool_max_size:
            oldest_key = next(iter(_exchange_pool))
            evicted = _exchange_pool.pop(oldest_key, None)
            _exchange_pool_health.pop(oldest_key, None)
            if evicted is not None:
                try:
                    close = getattr(evicted, "close", None)
                    if close:
                        close()
                except Exception as e:
                    logger.debug(f"[Exchange] Error closing evicted pool instance: {e}")

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
    margin_mode: str | None = None,
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

    # Set margin mode (cross/isolated) for contract trading
    effective_margin_mode = str(margin_mode or settings.risk.margin_mode or "cross").lower().strip()
    if effective_margin_mode not in ("cross", "isolated"):
        effective_margin_mode = "cross"
    options["defaultMarginMode"] = effective_margin_mode

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

    # Set margin mode on exchange instance for OKX and other exchanges that support it
    if effective_margin_mode == "isolated" and hasattr(exchange, "options"):
        exchange.options["defaultMarginMode"] = "isolated"

    return exchange


def _normalize_symbol(symbol: str) -> str:
    """Normalize symbol to exchange format.

    ENHANCED: Preserve .P suffix information for perpetual contract resolution.
    """
    if not symbol:
        return ""
    symbol = symbol.upper().replace(" ", "")

    # Remove .P/PERP suffix for normalization
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

    # Return normalized symbol (caller will use is_perpetual info if needed)
    return symbol


def _is_perpetual_ticker(ticker: str) -> bool:
    """Detect if ticker is a perpetual contract from TradingView format."""
    ticker_upper = str(ticker or "").upper().strip()
    return ticker_upper.endswith(".P") or ticker_upper.endswith("PERP")


def _valid_stop_loss(direction: SignalDirection, entry: float, price: float | None) -> float | None:
    """Compatibility helper shared by legacy tests and callers."""
    try:
        value = float(price or 0)
        entry = float(entry or 0)
    except (TypeError, ValueError):
        return None
    if value <= 0 or entry <= 0:
        return None
    min_distance_pct = 0.1
    distance_pct = abs(value - entry) / entry * 100 if entry > 0 else 100
    if distance_pct < min_distance_pct:
        logger.warning(f"[Exchange] Stop loss too close to entry ({distance_pct:.4f}% < {min_distance_pct}%), rejecting")
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


def _decision_take_profit_plan(decision: TradeDecision, status: str = "pending") -> list[dict[str, Any]]:
    """Serialize the final decision TP plan before exchange orders exist."""
    return [
        {
            "level": i + 1,
            "price": tp.price,
            "qty_pct": tp.qty_pct,
            "order_id": "",
            "status": status,
        }
        for i, tp in enumerate(decision.take_profit_levels)
    ]


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
    """Return common CCXT symbol candidates for a TradingView-style ticker.

    ENHANCED: Prioritize perpetual contract format for .P tickers.
    """
    raw_symbol = str(symbol or "").upper().replace(" ", "")

    # ENHANCED: Detect perpetual contract ticker
    is_perpetual = _is_perpetual_ticker(symbol)

    cleaned = _normalize_symbol(symbol).replace("/", "")
    quotes = ["USDT", "USDC", "BUSD", "USD", "BTC", "ETH", "BNB"]
    prefer_contract = _market_type_key(market_type) == "contract"

    # ENHANCED: Force contract preference for .P tickers
    if is_perpetual:
        prefer_contract = True

    candidates: list[str] = []
    if "/" in raw_symbol:
        candidates.append(raw_symbol)

    for quote in quotes:
        if cleaned.endswith(quote) and len(cleaned) > len(quote):
            base = cleaned[:-len(quote)]
            pair_symbol = f"{base}/{quote}"
            contract_symbol = f"{pair_symbol}:{quote}"

            # ENHANCED: For perpetual (.P) tickers, prioritize contract format
            if is_perpetual or prefer_contract:
                candidates.extend([contract_symbol, pair_symbol, f"{base}{quote}"])
            else:
                candidates.extend([pair_symbol, contract_symbol, f"{base}{quote}"])
            break
    else:
        pair_symbol = f"{cleaned}/USDT"
        contract_symbol = f"{pair_symbol}:USDT"

        # ENHANCED: For perpetual (.P) tickers, prioritize contract format
        if is_perpetual or prefer_contract:
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

    # ENHANCED: Fallback loop now respects market type to avoid returning spot when contract is requested
    for candidate in candidates:
        market = markets.get(candidate)
        if isinstance(market, dict) and _market_matches_type(market, target_market_type):
            return candidate

    # Fallback: scan all markets for matching ID with type check
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

    # ENHANCED: Only use fallback if it matches type, or log warning
    if fallback_symbol:
        fallback_market = markets.get(fallback_symbol)
        if fallback_market and _market_matches_type(fallback_market, target_market_type):
            return fallback_symbol
        logger.warning(
            f"[Exchange] Symbol {symbol} not found with requested type '{target_market_type}', "
            f"found '{fallback_symbol}' but it is {fallback_market.get('type', 'unknown')} market. "
            f"Using first candidate '{candidates[0]}' instead."
        )

    logger.warning(f"[Exchange] Symbol {symbol} not found in loaded markets; using {candidates[0]}")
    return candidates[0]


async def _fetch_market_max_leverage(exchange, symbol: str) -> float | None:
    """Query the exchange for the maximum allowed leverage for this symbol.

    Uses fetch_leverage_tiers() if supported, falls back to market limits.
    Results are cached per exchange+symbol to avoid repeated API calls.
    Returns None if the exchange doesn't expose leverage limits.
    """
    exchange_id = str(getattr(exchange, "id", "") or "").lower().strip()
    cache_key = f"{exchange_id}:{symbol}"
    cached = _MARKET_MAX_LEVERAGE_CACHE.get(cache_key)
    if cached is not None:
        return cached if cached > 0 else None

    max_lev = None

    try:
        tiers = await asyncio.to_thread(exchange.fetch_leverage_tiers, [symbol])
        if tiers and symbol in tiers:
            symbol_tiers = tiers[symbol]
            if symbol_tiers:
                # Each tier has maxLeverage; pick the highest across all tiers
                tier_maxes = [float(t.get("maxLeverage", 0)) for t in symbol_tiers]
                max_lev = max(tier_maxes) if tier_maxes else None
    except Exception:
        pass

    if not max_lev:
        try:
            market = exchange.market(symbol)
            lev_limit = market.get("limits", {}).get("leverage", {})
            max_lev = safe_float(lev_limit.get("max"))
        except Exception:
            pass

    _MARKET_MAX_LEVERAGE_CACHE[cache_key] = max_lev or 0.0
    if max_lev and max_lev > 0:
        logger.debug(f"[Exchange] Market max leverage for {symbol}: {max_lev}x (source: {exchange_id})")
    return max_lev if max_lev and max_lev > 0 else None


def _effective_order_leverage(decision: TradeDecision, exchange_config: dict | None = None) -> int | None:
    """Return the leverage that will actually be requested for this order."""
    exchange_config = exchange_config or {}
    if not decision.ai_analysis or not decision.ai_analysis.recommended_leverage:
        return None
    try:
        max_leverage = int(float(exchange_config.get("max_leverage") or 125))
    except (TypeError, ValueError):
        max_leverage = 125
    max_leverage = max(1, min(max_leverage, 125))
    return max(1, min(int(round(decision.ai_analysis.recommended_leverage)), max_leverage))


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
        return _simulate_order(decision, exchange_config)

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
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
    )
    symbol = await asyncio.to_thread(
        _resolve_symbol,
        exchange,
        decision.ticker,
        exchange_config.get("market_type") or settings.exchange.market_type,
    )

    try:
        leverage = _effective_order_leverage(decision, exchange_config)
        if leverage:
            # P2-FIX: Cap leverage to exchange's actual max for this symbol
            market_max = await _fetch_market_max_leverage(exchange, symbol)
            if market_max and market_max > 0 and leverage > market_max:
                original_leverage = leverage
                leverage = max(1, int(market_max))
                logger.warning(
                    f"[P2-FIX] Leverage capped: AI requested {original_leverage}x but "
                    f"{symbol} market max is {int(market_max)}x. Using {leverage}x."
                )
            # P0-FIX: Use retry mechanism for leverage setup
            result = await _set_leverage_with_retry(exchange, leverage, symbol)

            if not result["success"]:
                # Leverage setup failed
                if result.get("abort"):
                    # P0-FIX: Abort trade when leverage > 1x setup fails for safety
                    logger.error(
                        f"[P0-FIX] CRITICAL: Could not set requested leverage {leverage}x for {symbol}. "
                        f"{result.get('error', 'Unknown error')}. "
                        f"Aborting trade to prevent unintended risk exposure."
                    )
                    return {
                        "status": "error",
                        "reason": f"Leverage setup failed ({leverage}x): {result.get('error', 'Unknown')}. Trade aborted for safety.",
                    }
                else:
                    # Non-critical failure, continue with default leverage
                    logger.warning(f"[P0-FIX] Could not set leverage for {symbol}: {result.get('error', 'Unknown')}. Continuing with default leverage.")
            else:
                logger.info(f"[Exchange] Leverage set: {symbol} {leverage}x")

        if decision.direction in [SignalDirection.LONG]:
            side = "buy"
        elif decision.direction in [SignalDirection.SHORT]:
            side = "sell"
        elif decision.direction == SignalDirection.CLOSE_LONG:
            return await _close_position(exchange, symbol, position_side="long")
        elif decision.direction == SignalDirection.CLOSE_SHORT:
            return await _close_position(exchange, symbol, position_side="short")
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

        order_id = order.get("id")
        if not order_id:
            logger.warning(f"[Exchange] Order placed but returned no ID for {symbol}. Status: {order.get('status')}")
            return {
                "status": "error",
                "reason": "Exchange returned order without ID - cannot track position safely",
                "order_response": {k: v for k, v in order.items() if k not in {"info"}},
            }
        order_id = str(order_id)
        raw_status = order.get("status")
        order_status = raw_status if raw_status is not None else "open"
        actual_filled_qty = safe_float(order.get("filled") or 0)
        if raw_status is None and order_type == "limit":
            logger.info(f"[Exchange] OKX sandbox returned status=None for limit order {order_id}, treating as 'open' (pending)")
        requested_qty = safe_float(decision.quantity or 0)
        if actual_filled_qty == 0 and order_status in {"closed", "filled"}:
            actual_filled_qty = safe_float(order.get("amount") or 0)
            if actual_filled_qty == 0:
                logger.warning(f"[Exchange] Order {order_id} shows filled status but zero amount - treating as pending")
                order_status = "open"
                actual_filled_qty = 0
        is_partial_fill = (
            actual_filled_qty > 0
            and actual_filled_qty < requested_qty
        )
        actual_avg_price = safe_float(order.get("average") or order.get("price") or decision.entry_price or 0)
        logger.info(f"[Exchange] Entry order placed: {order_id} (status={order_status}, filled={actual_filled_qty}/{requested_qty})")

        if is_partial_fill:
            logger.warning(f"[Exchange] ⚠️ PARTIAL FILL: {actual_filled_qty}/{requested_qty} - placing TP/SL for filled portion only")

        result_status = (
            "pending" if order_type == "limit" and order_status in {"open", "new"} and actual_filled_qty == 0
            else "partial" if is_partial_fill
            else "filled" if order_status in {"closed", "filled"} or actual_filled_qty > 0
            else "ambiguous" if order_status in {"open", "new"} and order_type == "market"
            else "error"
        )
        if result_status == "ambiguous":
            logger.warning(f"[Exchange] Market order returned status={order_status}, may fill later. Waiting 3s...")
            await asyncio.sleep(3)
            try:
                order = await asyncio.to_thread(exchange.fetch_order, order_id, symbol)
                raw_status = order.get("status")
                order_status = raw_status if raw_status is not None else "open"
                actual_filled_qty = safe_float(order.get("filled") or 0)
                if actual_filled_qty == 0 and order_status in {"closed", "filled"}:
                    actual_filled_qty = safe_float(order.get("amount") or decision.quantity)
                is_partial_fill = (
                    actual_filled_qty > 0
                    and actual_filled_qty < requested_qty
                )
                result_status = (
                    "partial" if is_partial_fill
                    else "filled" if order_status in {"closed", "filled"} or actual_filled_qty > 0
                    else "error"
                )
                if result_status == "error":
                    logger.error(f"[Exchange] Market order still not filled after wait: {order_status}")
                    cancel_result = await _cancel_exchange_order(exchange, symbol, str(order_id))
                    return {
                        "status": "error",
                        "reason": f"Market order ambiguous after 3s: {order_status}",
                        "order_id": order_id,
                        "cancel_result": cancel_result,
                        "requires_reconciliation": True,
                    }
            except ccxt.OrderNotFound as e:
                logger.error(f"[Exchange] Re-fetch order not found: {e}")
                return {"status": "error", "reason": f"Order not found during verification: {e}", "order_id": order_id, "requires_reconciliation": True}
            except ccxt.NetworkError as e:
                logger.error(f"[Exchange] Network error re-fetching order: {e}")
                return {"status": "error", "reason": f"Network error verifying market order fill: {e}", "order_id": order_id, "requires_reconciliation": True}
            except Exception as e:
                logger.error(f"[Exchange] Failed to re-fetch order: {e}")
                return {"status": "error", "reason": f"Cannot verify market order fill: {e}", "order_id": order_id, "requires_reconciliation": True}
        if result_status == "error":
            logger.warning(f"[Exchange] Order status '{order_status}' treated as error")
            return {"status": "error", "reason": f"Order failed with status: {order_status}", "order_id": order_id}

        contract_size = 1.0
        try:
            ex_id = exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name
            mkt_type = exchange_config.get("market_type") or settings.exchange.market_type
            limits = get_market_limits(ex_id, decision.ticker, mkt_type)
            if limits and limits.get("contract_size", 1.0) > 1.0:
                contract_size = float(limits.get("contract_size", 1.0))
        except Exception:
            contract_size = 1.0

        result = {
            "status": result_status,
            "order_id": order_id,
            "symbol": symbol,
            "side": side,
            "quantity": actual_filled_qty if actual_filled_qty > 0 else decision.quantity,
            "requested_quantity": requested_qty,
            "entry_price": actual_avg_price if actual_avg_price > 0 else decision.entry_price,
            "sandbox_mode": sandbox_mode,
            "order_type": order_type,
            "exchange_order_status": order_status,
            "filled_quantity": actual_filled_qty,
            "is_partial_fill": is_partial_fill,
            "stop_loss": decision.stop_loss,
            "take_profit": decision.take_profit,
            "take_profit_orders": _decision_take_profit_plan(decision),
            # Notional value for correct margin calculation (handles contract markets)
            # Prefer exchange-reported cost, fallback to calculated notional with contract size
            "notional_value": safe_float(order.get("cost")) or (actual_filled_qty * actual_avg_price * contract_size),
            "contract_size": contract_size,
        }
        if leverage:
            result["recommended_leverage"] = leverage

        if decision.trailing_stop:
            result["trailing_stop_config"] = {
                "mode": decision.trailing_stop.mode.value,
                "trail_pct": decision.trailing_stop.trail_pct,
                "activation_profit_pct": decision.trailing_stop.activation_profit_pct,
                "trailing_step_pct": decision.trailing_stop.trailing_step_pct,
                "_ai_confidence": decision.ai_analysis.confidence if decision.ai_analysis else 0.65,
                "_ai_risk_score": decision.ai_analysis.risk_score if decision.ai_analysis else 0.5,
                "_ai_market_condition": decision.ai_analysis.market_condition if decision.ai_analysis else "unknown",
                "_ai_trend_strength": decision.ai_analysis.trend_strength if decision.ai_analysis else "moderate",
                "_signal_reasoning": decision.ai_analysis.reasoning if decision.ai_analysis else "",
                "_signal_timeframe": str(getattr(decision.signal, "timeframe", "60") or "60"),
            }

        # ── Multi Take-Profit Orders ──
        # Only place TP/SL for filled quantity
        # For pending limit orders, wait until filled (no protective orders yet)
        if result_status == "pending" and order_type == "limit":
            logger.info("[Exchange] Limit order pending, skipping TP/SL/trailing until filled")
            return result

        tp_qty = actual_filled_qty if actual_filled_qty > 0 else decision.quantity
        pos_side_for_orders = "long" if side == "buy" else "short"
        if decision.take_profit_levels and tp_qty > 0:
            tp_orders = await _place_multi_tp_orders(
                exchange, symbol, side, tp_qty, decision.take_profit_levels, position_side=pos_side_for_orders
            )
            result["take_profit_orders"] = tp_orders
            failed_tps = [tp for tp in tp_orders if tp.get("status") in {"error", "failed"}]
            if failed_tps:
                result["take_profit_error"] = f"Multi-TP failed: {len(failed_tps)}/{len(decision.take_profit_levels)} levels failed"
        elif decision.take_profit and tp_qty > 0:
            # Fallback: single TP order
            try:
                tp_side = "sell" if side == "buy" else "buy"
                tp_order = await _create_conditional_order(
                    exchange, symbol, "take_profit", tp_side, tp_qty, decision.take_profit, pos_side_for_orders
                )
                result["take_profit_order_id"] = tp_order.get("id")
                logger.info(f"[Exchange] ✅ Take-profit set at {decision.take_profit} (qty={tp_qty})")
            except Exception as e:
                logger.error(f"[Exchange] Failed to set take-profit: {e}")
                result["take_profit_error"] = str(e)

        # ── Stop-Loss / Trailing Stop ──
        trailing_mode = decision.trailing_stop.mode if decision.trailing_stop else TrailingStopMode.NONE
        sl_qty = actual_filled_qty if actual_filled_qty > 0 else decision.quantity

        if trailing_mode == TrailingStopMode.MOVING and sl_qty > 0:
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
                    amount=sl_qty,
                    params={
                        "callbackRate": callback_rate,
                        "closePosition": False,
                    },
                    position_side=pos_side_for_orders,
                )
                result["trailing_stop_order_id"] = ts_order.get("id")
                result["trailing_stop_mode"] = "moving"
                result["trailing_pct"] = trail_pct
                logger.info(f"[Exchange] ✅ Moving trailing stop set: {trail_pct}% (qty={sl_qty})")
            except Exception as e:
                logger.error(f"[Exchange] Failed to set trailing stop: {e}")
                result["trailing_stop_error"] = str(e)
                # Fallback to regular stop-loss
                if decision.stop_loss and sl_qty > 0:
                    await _place_stop_loss(exchange, symbol, side, sl_qty, decision.stop_loss, result, position_side=pos_side_for_orders)

        elif trailing_mode in (TrailingStopMode.BREAKEVEN_ON_TP1,
                                TrailingStopMode.STEP_TRAILING,
                                TrailingStopMode.PROFIT_PCT_TRAILING):
            # These modes require active monitoring; place initial SL now
            if decision.stop_loss and sl_qty > 0:
                await _place_stop_loss(exchange, symbol, side, sl_qty, decision.stop_loss, result, position_side=pos_side_for_orders)
            result["trailing_stop_mode"] = trailing_mode.value
            result["trailing_pct"] = decision.trailing_stop.trail_pct if decision.trailing_stop else 0
            result["trailing_activation_profit_pct"] = decision.trailing_stop.activation_profit_pct if decision.trailing_stop else 0
            result["trailing_stop_note"] = (
                "Initial SL placed. Trailing adjustments handled by position monitor."
            )
            logger.info(f"[Exchange] ⚡ Trailing mode '{trailing_mode.value}' active — initial SL placed (qty={sl_qty})")
        else:
            # No trailing: standard stop-loss
            if decision.stop_loss and sl_qty > 0:
                await _place_stop_loss(exchange, symbol, side, sl_qty, decision.stop_loss, result, position_side=pos_side_for_orders)

        # ── Protection Failure Check ──
        # If entry succeeded but SL/TP failed, close position for safety
        trailing_unprotected = result.get("trailing_stop_error") and not result.get("stop_loss_order_id")
        if result.get("status") in ("filled", "partial", "pending") and (
            result.get("stop_loss_error") or result.get("take_profit_error") or trailing_unprotected
):
            protection_errors = []
            if result.get("stop_loss_error"):
                protection_errors.append(f"SL: {result['stop_loss_error']}")
            if result.get("take_profit_error"):
                protection_errors.append(f"TP: {result['take_profit_error']}")
            if trailing_unprotected:
                protection_errors.append(f"Trailing: {result['trailing_stop_error']}")

            if result.get("status") in ("filled", "partial"):
                # Entry already filled - must close position
                # CRITICAL FIX: Cancel any remaining unfilled portion first
                if is_partial_fill and result.get("order_id"):
                    try:
                        cancel_result = await _cancel_exchange_order(exchange, symbol, str(result.get("order_id")))
                        logger.info(f"[Exchange] Cancelled unfilled entry portion: {cancel_result}")
                    except Exception as cancel_err:
                        logger.warning(f"[Exchange] Failed to cancel unfilled entry portion: {cancel_err}")

                logger.warning(
                    f"[Exchange] Protection orders failed for filled entry. "
                    f"Closing position {symbol} for safety. Errors: {protection_errors}"
                )
                try:
                    close_result = await _close_position(
                        exchange, symbol, position_side=pos_side_for_orders, close_quantity=actual_filled_qty
                    )
                    if close_result.get("status") == "closed":
                        return {
                            "status": "error",
                            "reason": "Entry filled but protection failed - position closed for safety",
                            "entry_order_id": result.get("order_id"),
                            "close_order_id": close_result.get("order_id"),
                            "exit_price": close_result.get("exit_price"),
                            "protection_errors": protection_errors,
                            "rollback_success": True,
                        }
                    else:
                        logger.error(f"[Exchange] CRITICAL: Failed to rollback unprotected position: {close_result}")
                        result["status"] = "partial_protection"
                        result["protection_errors"] = protection_errors
                        result["warning"] = "CRITICAL: Position opened but SL/TP failed - MANUAL STOP LOSS REQUIRED"
                        return result
                except ccxt.BaseError as rollback_err:
                    logger.error(f"[Exchange] CRITICAL: Rollback exception: {rollback_err}")
                    result["status"] = "partial_protection"
                    result["protection_errors"] = protection_errors
                    result["warning"] = "CRITICAL: Rollback failed - MANUAL STOP LOSS REQUIRED"
                    return result
                except Exception as rollback_err:
                    logger.error(f"[Exchange] CRITICAL: Unexpected rollback exception: {rollback_err}")
                    result["status"] = "partial_protection"
                    result["protection_errors"] = protection_errors
                    result["warning"] = "CRITICAL: Rollback failed - MANUAL STOP LOSS REQUIRED"
                    return result
            else:
                # Entry pending - cancel and return error
                logger.warning("[Exchange] Protection failed for pending entry, returning warning")
                result["protection_errors"] = protection_errors
                result["warning"] = "Protection orders failed - position pending"

        return result

    except ccxt.InsufficientFunds as e:
        logger.error(f"[Exchange] Insufficient funds: {e}")
        return {"status": "error", "reason": f"Insufficient funds: {e}"}
    except ccxt.NetworkError as e:
        logger.error(f"[Exchange] Network error: {e}")
        return {"status": "error", "reason": f"Network error: {e}"}
    except ccxt.BaseError as e:
        logger.error(f"[Exchange] Exchange error: {e}")
        return {"status": "error", "reason": f"Exchange error: {e}"}
    except Exception as e:
        logger.error(f"[Exchange] Order failed: {e}")
        return {"status": "error", "reason": f"Order execution failed: {e}"}


async def _place_stop_loss(exchange, symbol, side, quantity, stop_price, result, position_side: str | None = None):
    """Place a standard stop-loss order.

    Args:
        side: The entry order side (buy for long, sell for short)
        position_side: For OKX hedge mode, the position being protected.
    """
    try:
        sl_side = "sell" if side == "buy" else "buy"
        pos_side = position_side or ("long" if side == "buy" else "short")
        sl_order = await _create_conditional_order(exchange, symbol, "stop_loss", sl_side, quantity, stop_price, pos_side)
        result["stop_loss_order_id"] = sl_order.get("id")
        logger.info(f"[Exchange] ✅ Stop-loss set at {stop_price} (qty={quantity}, position_side={pos_side})")
    except ccxt.BaseError as e:
        logger.error(f"[Exchange] Failed to set stop-loss: {e}")
        result["stop_loss_error"] = "Failed to set stop-loss order"
    except Exception as e:
        logger.error(f"[Exchange] Unexpected error setting stop-loss: {e}")
        result["stop_loss_error"] = "Failed to set stop-loss order"


async def _place_multi_tp_orders(exchange, symbol, side, total_qty, tp_levels, position_side: str | None = None):
    """Place multiple take-profit orders at different price levels.

    Args:
        side: The entry order side (buy for long, sell for short)
        position_side: For OKX hedge mode, the position being protected.
    """
    tp_side = "sell" if side == "buy" else "buy"
    pos_side = position_side or ("long" if side == "buy" else "short")
    tp_results = []

    # Validate TP percentages to prevent overselling on partial fills
    total_qty_pct = sum(tp.qty_pct for tp in tp_levels)
    if total_qty_pct > 100:
        logger.warning(f"[Exchange] TP qty_pct sum {total_qty_pct}% exceeds 100%, normalizing to 100%")
        scale = 100.0 / total_qty_pct
        normalized_pcts = [tp.qty_pct * scale for tp in tp_levels]
    else:
        normalized_pcts = [tp.qty_pct for tp in tp_levels]

    for i, tp in enumerate(tp_levels):
        qty_pct = normalized_pcts[i]
        tp_qty = total_qty * (qty_pct / 100.0)
        if tp_qty <= 0:
            continue
        try:
            tp_order = await _create_conditional_order(
                exchange, symbol, "take_profit", tp_side, round(tp_qty, 6), tp.price, pos_side
            )
            tp_results.append({
                "level": i + 1,
                "price": tp.price,
                "qty": round(tp_qty, 6),
                "qty_pct": qty_pct,
                "order_id": tp_order.get("id"),
                "status": "placed",
                "position_side": pos_side,
            })
            logger.info(f"[Exchange] ✅ TP{i+1} set at {tp.price} ({qty_pct}% = {tp_qty}, position_side={pos_side})")
        except ccxt.BaseError as e:
            logger.error(f"[Exchange] Failed to set TP{i+1}: {e}")
            tp_results.append({
                "level": i + 1,
                "price": tp.price,
                "qty": round(tp_qty, 6),
                "qty_pct": qty_pct,
                "error": "Failed to place take-profit order",
                "status": "failed",
            })
        except Exception as e:
            logger.error(f"[Exchange] Unexpected error setting TP{i+1}: {e}")
            tp_results.append({
                "level": i + 1,
                "price": tp.price,
                "qty": round(tp_qty, 6),
                "qty_pct": qty_pct,
                "error": "Failed to place take-profit order",
                "status": "failed",
            })

    return tp_results


def _conditional_order_attempts(exchange_id: str, kind: str, trigger_price: float, position_side: str | None = None) -> list[tuple[str, dict[str, Any]]]:
    """Return exchange-aware conditional-order candidates.

    Args:
        position_side: For Bybit, determines triggerDirection. 'long' or 'short'.
                       LONG position: TP=rises(1), SL=falls(2)
                       SHORT position: TP=falls(2), SL=rises(1)
    """
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
        trigger_dir = _bybit_trigger_direction(kind, position_side)
        candidates.insert(0, ("market", {**reduce_params, "triggerPrice": trigger_price, "triggerDirection": trigger_dir}))
    return candidates


def _bybit_trigger_direction(kind: str, position_side: str | None) -> int:
    """Calculate Bybit triggerDirection based on order kind and position side.

    Bybit triggerDirection:
    - 1 = price rises to trigger price (for: LONG TP, SHORT SL)
    - 2 = price falls to trigger price (for: LONG SL, SHORT TP)
    """
    if not position_side:
        position_side = "long"
    pos_is_long = position_side.lower() == "long"
    if kind == "take_profit":
        return 1 if pos_is_long else 2
    else:
        return 2 if pos_is_long else 1


async def _create_conditional_order(exchange, symbol: str, kind: str, side: str, amount: float, trigger_price: float, position_side: str | None = None) -> dict:
    """Try exchange-specific conditional order formats before failing.

    Args:
        position_side: For OKX hedge mode, the position being protected ('long' or 'short').
                       For LONG position TP/SL: side=sell, position_side=long
                       For SHORT position TP/SL: side=buy, position_side=short
                       For Bybit, determines triggerDirection.
    """
    exchange_id = _exchange_id(exchange)
    errors = []
    for order_type, params in _conditional_order_attempts(exchange_id, kind, trigger_price, position_side):
        try:
            return await _create_exchange_order(
                exchange,
                symbol=symbol,
                order_type=order_type,
                side=side,
                amount=amount,
                params=params,
                position_side=position_side,
            )
        except ccxt.BaseError as exc:
            errors.append(f"{order_type}: {exc}")
            logger.debug(f"[Exchange] {exchange_id} {kind} candidate failed: {order_type} {exc}")
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
    except ccxt.OrderNotFound:
        return {"status": "not_found", "order_id": order_id, "symbol": symbol}
    except ccxt.NetworkError as exc:
        logger.error(f"[Exchange] Network error cancelling order {order_id} on {symbol}: {exc}")
        return {"status": "error", "order_id": order_id, "symbol": symbol, "reason": f"Network error: {exc}"}
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
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
    )
    try:
        symbol = await asyncio.to_thread(
            _resolve_symbol,
            exchange,
            ticker,
            exchange_config.get("market_type") or settings.exchange.market_type,
        )
        return await _cancel_exchange_order(exchange, symbol, order_id)
    except ccxt.BaseError as exc:
        logger.error(f"[Exchange] Failed to cancel order {order_id} for {ticker}: {exc}")
        return {"status": "error", "order_id": order_id, "ticker": ticker, "reason": str(exc)}
    except Exception as exc:
        logger.error(f"[Exchange] Unexpected error cancelling order {order_id} for {ticker}: {exc}")
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
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
    )
    try:
        symbol = await asyncio.to_thread(
            _resolve_symbol,
            exchange,
            ticker,
            exchange_config.get("market_type") or settings.exchange.market_type,
        )
        side = "sell" if str(direction).lower() == SignalDirection.LONG.value else "buy"
        pos_side_for_sl = "long" if str(direction).lower() in ("long", SignalDirection.LONG.value) else "short"
        order = await _create_conditional_order(exchange, symbol, "stop_loss", side, quantity, stop_price, pos_side_for_sl)
        result = {"status": "placed", "order_id": order.get("id"), "symbol": symbol, "stop_price": stop_price, "position_side": pos_side_for_sl}
        if existing_order_id:
            cancel_result = await _cancel_exchange_order(exchange, symbol, str(existing_order_id))
            result["replace_cancel_result"] = cancel_result
            if cancel_result.get("status") in {"cancelled", "not_found", "skipped"}:
                result["replaced_order_id"] = str(cancel_result.get("order_id") or existing_order_id)
            else:
                result["warning"] = cancel_result.get("reason") or "New stop placed but old stop could not be cancelled"
                logger.warning(
                    f"[Exchange] New protective stop placed for {symbol}, but old stop "
                    f"{existing_order_id} was not cancelled: {cancel_result}"
                )
        return result
    except ccxt.BaseError as e:
        logger.error(f"[Exchange] Failed to place protective stop: {e}")
        return {"status": "error", "reason": str(e)}
    except Exception as e:
        logger.error(f"[Exchange] Unexpected error placing protective stop: {e}")
        return {"status": "error", "reason": str(e)}


async def _close_position(exchange: ccxt.Exchange, symbol: str, position_side: str | None = None, close_quantity: float | None = None) -> dict:
    """Close an existing position.

    Args:
        position_side: For hedge mode exchanges (OKX), specify 'long' or 'short'.
                       If None, closes first found position (may be wrong in hedge mode).
        close_quantity: If specified, only close this quantity (for partial rollback).
                        If None, close entire position.
    """
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions, [symbol])
        for pos in positions:
            if pos["symbol"] != symbol:
                continue
            contracts = float(pos.get("contracts", 0))
            if contracts == 0:
                continue

            # In hedge mode, filter by position side
            pos_side = str(pos.get("side", "") or "").lower()
            if not pos_side:
                pos_info = pos.get("info") or {}
                pos_side = str(pos_info.get("posSide") or "").lower()

            # For net mode (no posSide), infer direction from contracts sign
            if not pos_side:
                pos_side = "long" if contracts > 0 else "short"
                contracts = abs(contracts)

            if position_side and pos_side and position_side.lower() not in pos_side:
                continue

            amount = abs(contracts)
            if close_quantity and close_quantity > 0:
                amount = min(amount, close_quantity)
            close_side = "sell" if pos_side == "long" else "buy"

            order = await _create_exchange_order(
                exchange,
                symbol=symbol,
                order_type="market",
                side=close_side,
                amount=amount,
                params={"reduceOnly": True},
                position_side=pos_side if pos_side else None,
                allow_amount_increase=False,
            )
            logger.info(f"[Exchange] ✅ Position closed: {order.get('id')} (side={pos_side or 'net'})")
            exit_price = order.get("average") or order.get("price") or pos.get("markPrice") or pos.get("entryPrice")
            return {"status": "closed", "order_id": order.get("id"), "exit_price": exit_price, "position_side": pos_side}
        return {"status": "no_position", "reason": f"No open {position_side or ''} position to close"}
    except ccxt.BaseError as e:
        logger.error(f"[Exchange] Failed to close position: {e}")
        return {"status": "error", "reason": f"Failed to close position: {e}"}
    except Exception as e:
        logger.error(f"[Exchange] Unexpected error closing position: {e}")
        return {"status": "error", "reason": "Failed to close position"}


def _calc_notional_value(quantity: float, price: float, ticker: str = "") -> float:
    """Calculate notional value for margin tracking.

    For spot markets: notional = quantity * price
    For contract markets: notional = quantity * price * contractSize

    Note: Contract size lookup is skipped here to avoid creating exchange
    instances. The quantity is already in contract count (set by
    _calculate_position_size), so callers should multiply by contract_size
    if known. This function returns the basic quantity * price as fallback.
    """
    if not quantity or not price or price <= 0:
        return 0.0
    return quantity * price


def _simulate_order(decision: TradeDecision, exchange_config: dict | None = None) -> dict:
    """Simulate order execution for paper trading with intelligent entry tracking."""
    exchange_config = exchange_config or {}
    tp_info = _decision_take_profit_plan(decision, status="simulated")
    leverage = _effective_order_leverage(decision, exchange_config)

    contract_size = 1.0
    try:
        limits = get_market_limits(
            exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
            decision.ticker,
            exchange_config.get("market_type") or settings.exchange.market_type,
        )
        contract_size = float(limits.get("contract_size", 1.0) or 1.0) if limits else 1.0
    except Exception:
        contract_size = 1.0

    notional_value = (
        float(decision.quantity or 0.0) * float(decision.entry_price or 0.0) * contract_size
        if decision.quantity and decision.entry_price
        else 0.0
    )

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
            "_ai_confidence": decision.ai_analysis.confidence if decision.ai_analysis else 0.65,
            "_ai_risk_score": decision.ai_analysis.risk_score if decision.ai_analysis else 0.5,
            "_ai_market_condition": decision.ai_analysis.market_condition if decision.ai_analysis else "unknown",
            "_ai_trend_strength": decision.ai_analysis.trend_strength if decision.ai_analysis else "moderate",
            "_signal_reasoning": decision.ai_analysis.reasoning if decision.ai_analysis else "",
            "_signal_timeframe": str(getattr(decision.signal, "timeframe", "60") or "60"),
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

    result = {
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
        # Notional value for correct margin calculation (handles contract markets)
        "notional_value": notional_value,
        "contract_size": contract_size,
    }
    if leverage:
        result["recommended_leverage"] = leverage
    return result


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
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
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
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
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
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
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
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
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
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
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
        if exchange_config.get("raise_on_error"):
            raise
        return []


async def get_open_orders(symbol: str | None = None, exchange_config: dict | None = None) -> list[dict]:
    """Fetch open/pending orders from exchange."""
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
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
    )
    try:
        if symbol:
            resolved_symbol = await asyncio.to_thread(
                _resolve_symbol,
                exchange,
                symbol,
                exchange_config.get("market_type") or settings.exchange.market_type,
            )
            orders = await asyncio.to_thread(exchange.fetch_open_orders, resolved_symbol)
        else:
            orders = await asyncio.to_thread(exchange.fetch_open_orders)

        return [
            {
                "id": o.get("id"),
                "symbol": o.get("symbol"),
                "side": o.get("side"),
                "type": o.get("type"),
                "price": o.get("price"),
                "amount": o.get("amount"),
                "filled": o.get("filled") or 0,
                "remaining": o.get("remaining") or o.get("amount") or 0,
                "status": o.get("status"),
                "timestamp": o.get("timestamp"),
                "datetime": o.get("datetime"),
            }
            for o in orders
        ]
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch open orders: {e}")
        if exchange_config.get("raise_on_error"):
            raise
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
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
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
                "remaining": o.get("remaining", max(0, (o.get("amount") or 0) - (o.get("filled") or 0))),
                "status": o.get("status"),
                "timestamp": o.get("timestamp"),
                "datetime": o.get("datetime"),
            }
            for o in orders
        ]
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch orders: {e}")
        if exchange_config.get("raise_on_error"):
            raise
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
