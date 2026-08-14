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
import uuid
from typing import Any

from loguru import logger

from core.config import settings
from core.exceptions import OrderValidationError
from core.utils.common import safe_bool as _safe_bool_common
from core.utils.common import safe_float as _safe_float_common
from models import SignalDirection, TradeDecision, TrailingStopMode

safe_float = _safe_float_common
safe_bool = _safe_bool_common

# P0-FIX: Leverage retry configuration
_LEVERAGE_MAX_RETRIES = 3
_LEVERAGE_RETRY_DELAY_BASE = 1.0
_LEVERAGE_RETRYABLE_ERRORS = ["NetworkError", "Timeout", "ExchangeNotAvailable", "DDoSProtection"]
_OKX_LEVERAGE_ERROR_CODES = ["11045", "51000", "51020"]
_MARKET_MAX_LEVERAGE_CACHE: dict[str, tuple[float, float]] = {}
_MARKET_MAX_LEVERAGE_TTL = 3600.0
_MARKET_MAX_LEVERAGE_LOCK = _threading.Lock()
_MARKET_MAX_LEVERAGE_CLEANUP_INTERVAL = 7200.0
_last_leverage_cleanup: float = 0.0
_EXCHANGE_IDLE_CLEANUP_SECS = 1800.0
_CLOSE_VERIFY_ATTEMPTS = 5
_CLOSE_VERIFY_DELAY_SECS = 0.75
_CLOSE_FLAT_CONTRACT_EPSILON = 1e-9
_LEVERAGE_SYMBOL_LOCKS: dict[str, asyncio.Lock] = {}
_LEVERAGE_LOCKS_GUARD = asyncio.Lock()

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


async def _close_exchange(exchange: ccxt.Exchange) -> None:
    close = getattr(exchange, "close", None)
    if not close:
        return
    try:
        if asyncio.iscoroutinefunction(close):
            await close()
        else:
            result = await asyncio.to_thread(close)
            if inspect.isawaitable(result):
                await result
    except Exception as e:
        logger.debug(f"[Exchange] Error closing exchange: {e}")


async def _get_leverage_symbol_lock(symbol: str) -> asyncio.Lock:
    async with _LEVERAGE_LOCKS_GUARD:
        lock = _LEVERAGE_SYMBOL_LOCKS.get(symbol)
        if lock is None:
            lock = asyncio.Lock()
            _LEVERAGE_SYMBOL_LOCKS[symbol] = lock
        return lock


def _is_okx_leverage_error(error_msg: str) -> tuple[bool, str]:
    """Check if error is an OKX leverage-related error (11045, 51000, 51020)."""
    text = str(error_msg).lower()
    for code in _OKX_LEVERAGE_ERROR_CODES:
        if f'"{code}"' in text or f"'{code}'" in text or f"code {code}" in text:
            return True, code
    return False, ""


def _okx_leverage_params(margin_mode: str, position_side: str | None = None) -> list[dict[str, str]]:
    base = {"tdMode": margin_mode}
    pos_side = str(position_side or "").lower().strip()
    if pos_side in {"long", "short"}:
        return [{**base, "posSide": pos_side}, base]
    return [base]


async def _set_leverage_once(
    exchange,
    exchange_id: str,
    leverage: int,
    symbol: str,
    margin_mode: str,
    position_side: str | None,
) -> dict[str, str] | None:
    if exchange_id != "okx":
        await asyncio.to_thread(exchange.set_leverage, leverage, symbol)
        return None

    last_pos_side_error: Exception | None = None
    for params in _okx_leverage_params(margin_mode, position_side):
        try:
            await asyncio.to_thread(exchange.set_leverage, leverage, symbol, params)
            return params
        except Exception as exc:
            is_okx_lev, okx_code = _is_okx_leverage_error(str(exc))
            if "posSide" in params and is_okx_lev and okx_code == "51000":
                last_pos_side_error = exc
                logger.warning(
                    f"[P0-FIX] OKX rejected leverage posSide={params['posSide']} for {symbol}; "
                    "retrying leverage setup without posSide for one-way/net mode compatibility."
                )
                continue
            raise

    if last_pos_side_error:
        raise last_pos_side_error
    return None


async def _set_leverage_with_retry(
    exchange,
    leverage: int,
    symbol: str,
    max_retries: int | None = None,
    position_side: str | None = None,
) -> dict:
    """P0-FIX: Set leverage with exponential backoff retry mechanism.

    Args:
        exchange: CCXT exchange instance
        leverage: Target leverage (e.g., 10 for 10x)
        symbol: Trading symbol (e.g., "BTC/USDT:USDT")
        max_retries: Maximum retry attempts (default: 3)
        position_side: OKX hedge-mode position side, "long" or "short"

    Returns:
        dict with "success": True/False and optional "error" message

    Retry Strategy:
        - Retries on transient errors (NetworkError, Timeout, DDoSProtection)
        - Exponential backoff: 1s, 2s, 4s
        - For OKX leverage errors (11045), tries switching margin mode (cross <-> isolated)
        - Does NOT retry on authentication errors or permanent exchange errors
        - Logs all attempts for observability
    """
    if max_retries is None:
        max_retries = (
            int(settings.order_execution.max_leverage_retry_attempts)
            if settings.order_execution.auto_retry_leverage_errors
            else 1
        )
    max_retries = max(1, int(max_retries))
    retry_delay_base = max(0.1, float(settings.order_execution.leverage_retry_delay_secs))

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
                used_params = await _set_leverage_once(
                    exchange,
                    exchange_id,
                    leverage,
                    symbol,
                    margin_mode,
                    position_side,
                )
                pos_side = used_params.get("posSide") if used_params else None
                logger.info(
                    f"[P0-FIX] Leverage set successfully: {symbol} {leverage}x "
                    f"(mode={margin_mode}, posSide={pos_side or 'net'}, "
                    f"attempt {attempt + 1}/{max_retries})"
                )
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
                    delay = retry_delay_base * (2 ** attempt)
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
                    delay = retry_delay_base * (2 ** attempt)
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


def _estimate_fill_price(orderbook: dict[str, Any], side: str, amount: float) -> float | None:
    """Walk the orderbook to estimate the volume-weighted average fill price.

    Used by pre-trade slippage protection: compares this estimate against
    the top-of-book reference price to detect thin-book / spoofed-depth
    situations BEFORE the order is sent.
    """
    if not orderbook or amount <= 0:
        return None
    book_side = "asks" if side == "buy" else "bids"
    levels = orderbook.get(book_side) or []
    if not levels:
        return None
    remaining = amount
    weighted_sum = 0.0
    for level in levels[:10]:
        price = float(level[0])
        qty = float(level[1]) if len(level) > 1 else 0.0
        if qty <= 0:
            continue
        fill = min(remaining, qty)
        weighted_sum += fill * price
        remaining -= fill
        if remaining <= 0:
            break
    if remaining > 1e-12:
        # Unknown depth must fail closed when pre-trade slippage protection is
        # enabled; extrapolating the last visible level understates risk.
        return None
    return weighted_sum / amount if amount > 0 else None


def _order_create_attempts(exchange, side: str, params: dict[str, Any] | None = None, position_side: str | None = None) -> list[dict[str, Any]]:
    base = dict(params or {})
    exchange_id = _exchange_id(exchange)

    if exchange_id == "bybit" and position_side:
        pos_idx = "1" if position_side.lower() == "long" else "2"
        base["positionIdx"] = pos_idx
        return [base]

    if exchange_id == "binance" and position_side:
        base["positionSide"] = position_side.upper()
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

    # P0-FIX: If position_side is explicitly provided (close/TP/SL), try with posSide first.
    # Only fallback to net-mode (no posSide) if the exchange rejects it for one-way mode.
    targeted = {**base, "tdMode": base.get("tdMode") or margin_mode, "posSide": pos_side}
    fallback = {**base, "tdMode": base.get("tdMode") or margin_mode}
    if position_side:
        return [targeted, fallback]
    return [fallback, targeted]


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
    client_order_id: str | None = None,
    reduce_only: bool = False,
    max_slippage_pct: float | None = None,
    slippage_reference_price: float | None = None,
    time_in_force: str | None = None,
    post_only: bool = False,
) -> dict:
    """Create an exchange order with small exchange-specific retries.

    Includes market precision and limits validation.

    Args:
        position_side: For OKX hedge mode, the actual position side ('long' or 'short').
                       Required for close/TP/SL orders to target correct position.
                       Optional for open orders (derived from side).
    """
    from core.metrics import EXCHANGE_ERRORS, record_exchange_request

    # Validate amount against market limits before placing order
    requested_amount = amount
    amount = _validate_and_adjust_amount(exchange, symbol, amount, allow_amount_increase)
    if not allow_amount_increase and amount > requested_amount:
        raise ValueError(
            f"Adjusted close amount {amount} exceeds requested rollback amount {requested_amount}"
        )

    exchange_id = _exchange_id(exchange)
    errors: list[str] = []

    if max_slippage_pct is not None and order_type == "market" and price is None:
        try:
            threshold = float(max_slippage_pct)
            if not math.isfinite(threshold) or threshold <= 0:
                raise OrderValidationError(
                    f"Invalid max_slippage_pct={max_slippage_pct!r}; live market order blocked"
                )
            orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol, limit=20)
            book_side = "asks" if side == "buy" else "bids"
            levels = (orderbook or {}).get(book_side) or []
            if not levels:
                raise OrderValidationError(
                    f"Slippage protection unavailable: no {book_side} for {symbol}"
                )
            top_of_book = float(levels[0][0])
            reference_price = float(slippage_reference_price or top_of_book)
            if reference_price <= 0:
                raise OrderValidationError(
                    f"Slippage protection unavailable: invalid reference price for {symbol}"
                )
            estimated_fill = _estimate_fill_price(orderbook, side, amount)
            if estimated_fill is None or estimated_fill <= 0:
                raise OrderValidationError(
                    f"Slippage protection unavailable: visible {book_side} depth "
                    f"cannot fill {amount} {symbol}"
                )
            if side == "buy":
                slippage_pct = max(
                    0.0,
                    (estimated_fill - reference_price) / reference_price * 100.0,
                )
            else:
                slippage_pct = max(
                    0.0,
                    (reference_price - estimated_fill) / reference_price * 100.0,
                )
            if slippage_pct > threshold:
                logger.error(
                    f"[Exchange] ABORT ORDER {symbol} {side}: estimated adverse slippage "
                    f"{slippage_pct:.3f}% exceeds max {threshold:.3f}% "
                    f"(ref={reference_price} est_fill={estimated_fill})"
                )
                raise OrderValidationError(
                    f"Slippage protection aborted order: estimated {slippage_pct:.3f}% "
                    f"> max {threshold:.3f}%"
                )
            logger.info(
                f"[Exchange] Slippage protection OK: {symbol} {side} "
                f"est_slippage={slippage_pct:.3f}% max={threshold:.3f}%"
            )
        except OrderValidationError:
            raise
        except Exception as exc:
            raise OrderValidationError(
                f"Slippage protection check failed for {symbol}: {exc}"
            ) from exc

    for attempt_params in _order_create_attempts(exchange, side, params, position_side):
        if client_order_id:
            attempt_params = dict(attempt_params) if attempt_params else {}
            attempt_params["clientOrderId"] = client_order_id
        if reduce_only:
            attempt_params["reduceOnly"] = True
        if time_in_force:
            attempt_params["timeInForce"] = time_in_force
        if post_only:
            attempt_params["postOnly"] = True
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
            if isinstance(result, dict):
                result.setdefault("_requested_amount", requested_amount)
                result.setdefault("_submitted_amount", amount)
            return result
        except ccxt.NetworkError as exc:
            latency = time.time() - start
            record_exchange_request(
                exchange=exchange_id,
                endpoint="create_order",
                status="error",
                latency=latency,
            )
            EXCHANGE_ERRORS.labels(exchange=exchange_id, error_type=type(exc).__name__).inc()
            # A network failure after create_order starts is ambiguous: the
            # exchange may have accepted it. Never try a different order
            # format from this call site.
            raise
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


def _validate_and_adjust_amount(exchange, symbol: str, amount: float, allow_increase: bool = True) -> float:
    """
    Validate and adjust order amount against exchange market limits.

    Handles:
    - Minimum order amount (e.g., XAU requires min 1 unit)
    - Maximum order amount (e.g., SHIB has max limit per order)
    - Amount precision (e.g., some markets require integer amounts)

    Args:
        allow_increase: If False, do NOT increase amount above the requested value.
                        Used for close/rollback orders to prevent closing more than
                        the actual position.

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
            if allow_increase:
                logger.warning(
                    f"[Exchange] Amount {amount} < min_amount {min_amount} for {symbol}, "
                    f"adjusting to minimum"
                )
                amount = min_amount
            else:
                raise ValueError(
                    f"Amount {amount} < min_amount {min_amount} for {symbol}, "
                    f"cannot increase for close order"
                )

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

    except ValueError:
        raise
    except Exception as e:
        logger.warning(f"[Exchange] Could not validate amount for {symbol}: {e}")
        return amount


async def create_reduce_order(
    exchange,
    symbol: str,
    side: str,
    amount: float,
    position_side: str | None = None,
    client_order_id: str | None = None,
) -> dict:
    """Create a reduce-only order for position closing.

    Ensures the order only reduces an existing position, never opens a new one.
    """
    return await _create_exchange_order(
        exchange=exchange,
        symbol=symbol,
        order_type="market",
        side=side,
        amount=amount,
        position_side=position_side,
        reduce_only=True,
        client_order_id=client_order_id,
        allow_amount_increase=False,
    )


async def create_limit_order_with_protection(
    exchange,
    symbol: str,
    side: str,
    amount: float,
    price: float,
    position_side: str | None = None,
    post_only: bool = False,
    time_in_force: str | None = None,
    client_order_id: str | None = None,
) -> dict:
    """Create a limit order with optional post-only and time-in-force."""
    return await _create_exchange_order(
        exchange=exchange,
        symbol=symbol,
        order_type="limit",
        side=side,
        amount=amount,
        price=price,
        position_side=position_side,
        post_only=post_only,
        time_in_force=time_in_force,
        client_order_id=client_order_id,
    )


async def estimate_execution_slippage(
    exchange,
    symbol: str,
    side: str,
    amount: float,
    price: float,
) -> dict[str, Any]:
    """Estimate execution slippage before placing order."""
    try:
        orderbook = await asyncio.to_thread(exchange.fetch_order_book, symbol, limit=20)
        if not orderbook:
            return {"slippage_pct": None, "recommendation": "no_data"}

        book = orderbook.get("asks", []) if side == "buy" else orderbook.get("bids", [])
        if not book:
            return {"slippage_pct": None, "recommendation": "no_data"}

        remaining_cost = amount * price
        filled_cost = 0.0
        filled_qty = 0.0

        for level in book:
            level_price = float(level[0])
            level_qty = float(level[1])
            level_cost = level_price * level_qty
            fill = min(remaining_cost - filled_cost, level_cost)
            qty = fill / level_price
            filled_cost += fill
            filled_qty += qty
            if filled_cost >= remaining_cost:
                break

        if filled_qty <= 0:
            return {"slippage_pct": None, "recommendation": "insufficient_liquidity"}

        avg_price = filled_cost / filled_qty
        slippage_pct = abs(avg_price - price) / price * 100

        recommendation = "ok"
        if slippage_pct > 1.0:
            recommendation = "high_slippage_reduce_size"
        elif slippage_pct > 0.5:
            recommendation = "moderate_slippage_caution"

        return {
            "slippage_pct": round(slippage_pct, 4),
            "avg_fill_price": round(avg_price, 8),
            "recommendation": recommendation,
        }
    except Exception as e:
        logger.debug(f"[Exchange] Slippage estimation failed for {symbol}: {e}")
        return {"slippage_pct": None, "recommendation": "error"}


def calculate_worst_case_loss(
    entry_price: float,
    stop_loss: float,
    quantity: float,
    leverage: float = 1.0,
) -> dict[str, float]:
    """Calculate worst-case loss for a trade before execution."""
    if entry_price <= 0 or quantity <= 0:
        return {"loss_usdt": 0.0, "loss_pct": 0.0}

    sl_distance = abs(entry_price - stop_loss) if stop_loss > 0 else entry_price * 0.02
    loss_usdt = sl_distance * quantity * leverage
    position_value = entry_price * quantity
    loss_pct = (loss_usdt / position_value * 100.0) if position_value > 0 else 0.0

    return {
        "loss_usdt": round(loss_usdt, 4),
        "loss_pct": round(loss_pct, 2),
        "sl_distance_pct": round(sl_distance / entry_price * 100.0, 4) if entry_price > 0 else 0.0,
    }


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
        target_market_type = _market_type_key(market_type)
        for candidate in candidates:
            market = markets.get(candidate)
            if isinstance(market, dict) and _market_matches_type(market, target_market_type):
                resolved_symbol = candidate
                break

        if not isinstance(market, dict):
            logger.warning(
                f"[Exchange] Market {symbol} not found for requested type '{target_market_type}' "
                f"in {exchange_id} (tried: {candidates})"
            )
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

        if contract_size != 1.0:
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

    requested_qty = quantity

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
            f"original={requested_qty}, final={quantity}, adjustments: {', '.join(adjustments)}"
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


def client_order_id_for_idempotency(idempotency_key: str) -> str:
    """Build the exchange client ID used for an idempotent order submission."""
    digest = _hashlib.sha256(str(idempotency_key).encode("utf-8")).hexdigest()[:24]
    return f"qp_{digest}"


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

    Health checks are performed OUTSIDE the lock to avoid blocking
    other threads during network I/O.
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
            with _exchange_pool_lock:
                removed = _exchange_pool.pop(cache_key, None)
                _exchange_pool_health.pop(cache_key, None)
            if removed is not None:
                try:
                    close = getattr(removed, "close", None)
                    if close:
                        close()
                except Exception as e:
                    logger.debug(f"[Exchange] Error closing evicted cached instance: {e}")
        elif now - last_check > _HEALTH_CHECK_INTERVAL_SECS:
            needs_rebuild = False
            try:
                existing.fetch_time()
                with _exchange_pool_lock:
                    _exchange_pool_health[cache_key] = {
                        "last_check": time.time(),
                        "consecutive_failures": 0,
                    }
                return existing
            except Exception as exc:
                logger.warning(f"[Exchange] Health check failed for {cache_key}: {exc}")
                removed = None
                with _exchange_pool_lock:
                    current_health = _exchange_pool_health.get(cache_key, {})
                    new_consecutive = current_health.get("consecutive_failures", 0) + 1
                    if new_consecutive >= _MAX_CONSECUTIVE_FAILURES:
                        removed = _exchange_pool.pop(cache_key, None)
                        _exchange_pool_health.pop(cache_key, None)
                        needs_rebuild = True
                    else:
                        _exchange_pool_health[cache_key] = {
                            "last_check": time.time(),
                            "consecutive_failures": new_consecutive,
                        }

                if removed is not None:
                    try:
                        close = getattr(removed, "close", None)
                        if close:
                            close()
                    except Exception as e:
                        logger.debug(f"[Exchange] Error closing unhealthy cached instance: {e}")

                if needs_rebuild:
                    logger.info(f"[Exchange] Health check failed, rebuilding instance for {cache_key}")
                    with _exchange_pool_lock:
                        if cache_key in _exchange_pool:
                            removed = _exchange_pool.pop(cache_key, None)
                            _exchange_pool_health.pop(cache_key, None)
                        instance = _build_exchange(exchange_id, api_key, api_secret, password, live, sandbox, market_type, margin_mode)
                        if len(_exchange_pool) >= settings.exchange.pool_max_size:
                            if _exchange_pool:
                                oldest_key = next(iter(_exchange_pool))
                                evicted = _exchange_pool.pop(oldest_key, None)
                                _exchange_pool_health.pop(oldest_key, None)
                            else:
                                evicted = None
                            if evicted is not None:
                                try:
                                    close = getattr(evicted, "close", None)
                                    if close:
                                        close()
                                except Exception:
                                    pass
                        _exchange_pool[cache_key] = instance
                        _exchange_pool_health[cache_key] = {"last_check": time.time(), "consecutive_failures": 0}
                        return instance
        else:
            return existing

    with _exchange_pool_lock:
        existing = _exchange_pool.get(cache_key)
        if existing is not None:
            return existing

        instance = _build_exchange(exchange_id, api_key, api_secret, password, live, sandbox, market_type, margin_mode)

        if len(_exchange_pool) >= settings.exchange.pool_max_size:
            if _exchange_pool:
                oldest_key = next(iter(_exchange_pool))
                evicted = _exchange_pool.pop(oldest_key, None)
                _exchange_pool_health.pop(oldest_key, None)
            else:
                evicted = None
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


def cleanup_idle_exchange_pool(max_idle_secs: float = _EXCHANGE_IDLE_CLEANUP_SECS) -> int:
    """Close and remove exchange instances that haven't been used recently.

    Returns the number of connections cleaned up.
    """
    cleaned = 0
    now = time.time()
    with _exchange_pool_lock:
        stale_keys = []
        for key, health in _exchange_pool_health.items():
            last_check = health.get("last_check", 0)
            if now - last_check > max_idle_secs:
                stale_keys.append(key)
        for key in stale_keys:
            removed = _exchange_pool.pop(key, None)
            _exchange_pool_health.pop(key, None)
            if removed is not None:
                try:
                    close = getattr(removed, "close", None)
                    if close:
                        close()
                except Exception:
                    pass
                cleaned += 1
        # Clean up stale health entries not in pool
        stale_health = [k for k in _exchange_pool_health if k not in _exchange_pool]
        for k in stale_health:
            _exchange_pool_health.pop(k, None)
    if cleaned:
        logger.info(f"[Exchange] Cleaned up {cleaned} idle exchange connections")
    return cleaned


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
        return bool(market.get("spot") is True)
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
        raise ValueError(f"Could not load markets for symbol resolution: {e}") from e

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

    if fallback_symbol:
        fallback_market = markets.get(fallback_symbol)
        if fallback_market and _market_matches_type(fallback_market, target_market_type):
            return fallback_symbol
        raise ValueError(
            f"[Exchange] Symbol {symbol} not found with requested type '{target_market_type}'. "
            f"Found '{fallback_symbol}' but it is a {fallback_market.get('type', 'unknown') if fallback_market else 'unknown'} market. "
            "Trade aborted to avoid executing on the wrong market type."
        )

    raise ValueError(f"[Exchange] Symbol {symbol} not found in loaded markets for requested type '{target_market_type}'")


async def _fetch_market_max_leverage(exchange, symbol: str) -> float | None:
    """Query the exchange for the maximum allowed leverage for this symbol.

    Uses TTL-based caching with thread-safe access.
    P1-FIX: Periodic cleanup of expired entries to prevent memory leak.
    Returns None if the exchange doesn't expose leverage limits.
    """
    global _last_leverage_cleanup
    exchange_id = str(getattr(exchange, "id", "") or "").lower().strip()
    cache_key = f"{exchange_id}:{symbol}"
    now = time.time()

    # P1-FIX: Periodic cleanup of expired entries
    if now - _last_leverage_cleanup > _MARKET_MAX_LEVERAGE_CLEANUP_INTERVAL:
        with _MARKET_MAX_LEVERAGE_LOCK:
            expired_keys = [
                k for k, (v, t) in _MARKET_MAX_LEVERAGE_CACHE.items()
                if now - t >= _MARKET_MAX_LEVERAGE_TTL
            ]
            for k in expired_keys:
                _MARKET_MAX_LEVERAGE_CACHE.pop(k, None)
            _last_leverage_cleanup = now

    with _MARKET_MAX_LEVERAGE_LOCK:
        cached = _MARKET_MAX_LEVERAGE_CACHE.get(cache_key)
        if cached is not None:
            cached_val, cached_at = cached
            if now - cached_at < _MARKET_MAX_LEVERAGE_TTL:
                return cached_val if cached_val > 0 else None

    max_lev = None

    try:
        tiers = await asyncio.to_thread(exchange.fetch_leverage_tiers, [symbol])
        if tiers and symbol in tiers:
            symbol_tiers = tiers[symbol]
            if symbol_tiers:
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

    with _MARKET_MAX_LEVERAGE_LOCK:
        _MARKET_MAX_LEVERAGE_CACHE[cache_key] = (max_lev or 0.0, now)

    if max_lev and max_lev > 0:
        logger.debug(f"[Exchange] Market max leverage for {symbol}: {max_lev}x (source: {exchange_id})")
    return max_lev if max_lev and max_lev > 0 else None


def _effective_order_leverage(decision: TradeDecision, exchange_config: dict | None = None) -> int | None:
    """Return the leverage that will actually be requested for this order."""
    exchange_config = exchange_config or {}
    if not decision.ai_analysis or not decision.ai_analysis.recommended_leverage:
        return None
    try:
        raw_max = exchange_config.get("max_leverage") or 125
        max_leverage = int(float(raw_max))
        if max_leverage <= 0 or math.isnan(max_leverage):
            max_leverage = 125
    except (TypeError, ValueError, OverflowError):
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
    live_trading = safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False)
    sandbox_mode = safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False)
    is_close_order = decision.direction in {SignalDirection.CLOSE_LONG, SignalDirection.CLOSE_SHORT}

    if not live_trading:
        logger.warning("[Exchange] 🔶 PAPER TRADING MODE - not sending real orders")
        return _simulate_order(decision, exchange_config)

    if not safe_bool(settings.exchange.live_trading, False) and not is_close_order:
        return {
            "status": "rejected",
            "reason": "Global LIVE_TRADING=false blocks live entry orders",
        }

    requested_order_type = str(getattr(decision, "order_type", "") or "market").strip().lower()
    if not is_close_order and requested_order_type == "limit":
        # Reject before creating an exchange client or changing leverage: this
        # path cannot atomically bind an entry fill to its protective orders.
        return {
            "status": "rejected",
            "reason": (
                "Live limit entries are disabled because stop-loss protection "
                "cannot be attached atomically; use a market entry"
            ),
            "retry_safe": False,
            "failure_stage": "pre_execution",
        }

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

    original_leverage = None
    leverage_changed = False
    leverage_position_side = None
    if decision.direction in [SignalDirection.LONG, SignalDirection.CLOSE_LONG]:
        leverage_position_side = "long"
    elif decision.direction in [SignalDirection.SHORT, SignalDirection.CLOSE_SHORT]:
        leverage_position_side = "short"

    lev_lock = await _get_leverage_symbol_lock(symbol)
    if decision.idempotency_key:
        client_order_id = client_order_id_for_idempotency(decision.idempotency_key)
    else:
        client_order_id = f"qp_{uuid.uuid4().hex[:16]}"
    order_submission_started = False

    try:
        leverage = None if is_close_order else _effective_order_leverage(decision, exchange_config)
        if leverage:
            async with lev_lock:
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
                result = await _set_leverage_with_retry(
                    exchange,
                    leverage,
                    symbol,
                    position_side=leverage_position_side,
                )

                if not result["success"]:
                    if result.get("abort"):
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
                        logger.error(f"[Exchange] Could not verify 1x leverage for {symbol}: {result.get('error', 'Unknown')}. "
                                    f"Aborting trade for safety — unknown exchange default leverage could exceed 1x.")
                        return {
                            "status": "error",
                            "reason": f"Cannot verify 1x leverage for {symbol}. Trade aborted for safety.",
                        }
                else:
                    leverage_changed = True
                    logger.info(f"[Exchange] Leverage set: {symbol} {leverage}x")

        if decision.direction in [SignalDirection.LONG]:
            side = "buy"
        elif decision.direction in [SignalDirection.SHORT]:
            side = "sell"
        elif decision.direction == SignalDirection.CLOSE_LONG:
            return await _close_position(
                exchange,
                symbol,
                position_side="long",
                close_quantity=decision.quantity if decision.quantity and decision.quantity > 0 else None,
                client_order_id=client_order_id,
            )
        elif decision.direction == SignalDirection.CLOSE_SHORT:
            return await _close_position(
                exchange,
                symbol,
                position_side="short",
                close_quantity=decision.quantity if decision.quantity and decision.quantity > 0 else None,
                client_order_id=client_order_id,
            )
        else:
            return {"status": "error", "reason": f"Unknown direction: {decision.direction}"}

        if decision.quantity is None or decision.quantity <= 0:
            return {"status": "error", "reason": "Quantity must be greater than zero"}

        # Support both market and limit orders
        order_type = str(getattr(decision, "order_type", "") or "").strip().lower()
        if not order_type or order_type not in ("market", "limit"):
            order_type = "market"
        max_slippage_pct = float(
            exchange_config.get("max_slippage_pct")
            or settings.risk.max_slippage_pct
            or 1.0
        )

        try:
            # P1-FIX: Validate live entry amount increase tolerance (max 5% above requested)
            requested_qty = decision.quantity
            if order_type == "limit" and decision.entry_price and decision.entry_price > 0:
                logger.info(f"[Exchange] Placing {side} LIMIT order: {symbol} qty={decision.quantity} @ {decision.entry_price}")
                order_submission_started = True
                order = await _create_exchange_order(
                    exchange,
                    symbol=symbol,
                    order_type="limit",
                    side=side,
                    amount=decision.quantity,
                    price=decision.entry_price,
                    allow_amount_increase=False,
                    client_order_id=client_order_id,
                )
            else:
                logger.info(f"[Exchange] Placing {side} MARKET order: {symbol} qty={decision.quantity}")
                order_submission_started = True
                order = await _create_exchange_order(
                    exchange,
                    symbol=symbol,
                    order_type="market",
                    side=side,
                    amount=decision.quantity,
                    allow_amount_increase=False,
                    client_order_id=client_order_id,
                    max_slippage_pct=max_slippage_pct,
                    slippage_reference_price=decision.entry_price,
                )
        except (ccxt.BaseError, Exception) as order_exc:
            logger.error(f"[Exchange] Order placement failed for {symbol}: {order_exc}")
            if leverage_changed and leverage and leverage > 1:
                logger.warning(
                    f"[Exchange] Attempting leverage rollback for {symbol} after order failure"
                )
                try:
                    rollback_leverage = 1
                    await _set_leverage_with_retry(
                        exchange,
                        rollback_leverage,
                        symbol,
                        position_side=leverage_position_side,
                    )
                    logger.info(f"[Exchange] Leverage rolled back to {rollback_leverage}x for {symbol}")
                except Exception as rollback_exc:
                    logger.warning(
                        f"[Exchange] Leverage rollback also failed for {symbol}: {rollback_exc}. "
                        f"Manual intervention may be required."
                    )
            raise

        order_id = order.get("id")
        if not order_id:
            logger.warning(f"[Exchange] Order placed but returned no ID for {symbol}. Status: {order.get('status')}")
            return {
                # The exchange call returned successfully, so this is not a
                # retry-safe failure.  Keep the deterministic client ID and
                # force reconciliation to prevent an automatic duplicate.
                "status": "manual_review",
                "reason": "Exchange returned order without ID - cannot track position safely",
                "client_order_id": client_order_id,
                "accepted_without_id": True,
                "requires_reconciliation": True,
                "retry_safe": False,
                "failure_stage": "post_submission",
                "order_response": {k: v for k, v in order.items() if k not in {"info"}},
            }
        order_id = str(order_id)
        raw_status = order.get("status")
        # P0-FIX: For limit orders, status=None means "not yet filled" (pending),
        # NOT "open/filled". OKX Sandbox returns None for unfilled limit orders.
        # Treating None as "open" caused Ghost Position detection to trigger
        # prematurely, killing limit orders before their timeout expired.
        if raw_status is None and order_type == "limit":
            order_status = "open"  # CCXT convention: open = waiting to fill
            logger.info(f"[Exchange] OKX sandbox returned status=None for limit order {order_id}, treating as 'open' (pending fill)")
        elif raw_status is None:
            order_status = "open"
            logger.warning(f"[Exchange] Order {order_id} returned status=None (type={order_type}), treating as 'open'")
        else:
            order_status = raw_status
        submitted_qty = safe_float(order.get("_submitted_amount") or order.get("amount") or decision.quantity or 0)
        actual_filled_qty = safe_float(order.get("filled") or 0)
        if raw_status is None and order_type == "limit":
            logger.info(f"[Exchange] OKX sandbox returned status=None for limit order {order_id}, treating as 'open' (pending)")
        requested_qty = safe_float(order.get("_requested_amount") or decision.quantity or 0)
        if actual_filled_qty == 0 and order_status in {"closed", "filled"}:
            actual_filled_qty = safe_float(order.get("amount") or submitted_qty or 0)
            if actual_filled_qty == 0:
                logger.warning(f"[Exchange] Order {order_id} shows filled status but zero amount - treating as pending")
                order_status = "open"
                actual_filled_qty = 0
        is_partial_fill = (
            actual_filled_qty > 0
            and actual_filled_qty < submitted_qty
        )
        actual_avg_price = safe_float(order.get("average") or order.get("price") or decision.entry_price or 0)
        logger.info(f"[Exchange] Entry order placed: {order_id} (status={order_status}, filled={actual_filled_qty}/{requested_qty})")

        if order_type == "market" and actual_avg_price > 0 and decision.entry_price and decision.entry_price > 0:
            if side == "buy":
                slippage_pct = max(
                    0.0,
                    (actual_avg_price - decision.entry_price) / decision.entry_price * 100,
                )
            else:
                slippage_pct = max(
                    0.0,
                    (decision.entry_price - actual_avg_price) / decision.entry_price * 100,
                )
            if slippage_pct > max_slippage_pct:
                # The order-book walk is only an estimate. Enforce the same
                # adverse-slippage threshold on the actual fill.
                logger.error(
                    f"[Exchange] CRITICAL SLIPPAGE {slippage_pct:.4f}% (> max {max_slippage_pct}%) for {symbol}. "
                    f"Expected: {decision.entry_price}, Got: {actual_avg_price}. Initiating emergency reduce-only close."
                )
                try:
                    from notifier import notify_error
                    await notify_error(
                        f"CRITICAL SLIPPAGE on {symbol} {decision.direction}: {slippage_pct:.3f}% "
                        f"(expected {decision.entry_price}, filled {actual_avg_price}). "
                        f"Auto-closing position to limit damage."
                    )
                except Exception:
                    pass
                close_result: dict[str, Any] = {}
                try:
                    pos_side = "long" if decision.direction == SignalDirection.LONG else "short"
                    close_result = await _close_position(
                        exchange, symbol, position_side=pos_side,
                        close_quantity=actual_filled_qty,
                        client_order_id=f"qp_slipclose_{int(time.time())}",
                    )
                except Exception as close_err:
                    logger.error(f"[Exchange] Emergency slippage close FAILED for {symbol}: {close_err}")
                    close_result = {"status": "error", "reason": str(close_err)}
                rollback_success = str(close_result.get("status") or "").lower() == "closed"
                # Return as error so the trade flow doesn't proceed to place TP/SL
                return {
                    "status": "error",
                    "error": f"slippage_exceeded_{slippage_pct:.2f}pct",
                    "order_id": str(order_id),
                    "client_order_id": client_order_id,
                    "filled_quantity": actual_filled_qty,
                    "avg_price": actual_avg_price,
                    "emergency_closed": rollback_success,
                    "rollback_success": rollback_success,
                    "requires_reconciliation": not rollback_success,
                    "emergency_close_result": close_result,
                }

        partial_fill_cancel_confirmed = not is_partial_fill
        protection_qty = actual_filled_qty if actual_filled_qty > 0 else submitted_qty
        partial_fill_cancel_result: dict[str, Any] | None = None
        if is_partial_fill:
            logger.warning(f"[Exchange] ⚠️ PARTIAL FILL: {actual_filled_qty}/{requested_qty} - cancelling unfilled portion")
            try:
                partial_fill_cancel_result = await _cancel_exchange_order(exchange, symbol, str(order_id))
                # OrderNotFound is ambiguous for a partially-filled entry: it
                # may have filled and moved to the exchange archive. Only an
                # explicit cancellation response is immediate confirmation.
                partial_fill_cancel_confirmed = (
                    partial_fill_cancel_result.get("status") == "cancelled"
                )
                if partial_fill_cancel_confirmed:
                    logger.info(f"[Exchange] Cancelled unfilled entry portion: {partial_fill_cancel_result}")
                else:
                    logger.error(
                        f"[Exchange] Could not confirm cancellation of partially-filled entry "
                        f"{order_id}: {partial_fill_cancel_result}"
                    )
            except Exception as cancel_err:
                logger.error(f"[Exchange] Failed to cancel unfilled entry portion: {cancel_err}")

            if not partial_fill_cancel_confirmed:
                # Cancellation can race another fill. Re-read the order before
                # deciding how much protection is required.
                try:
                    refreshed_order = await asyncio.to_thread(exchange.fetch_order, order_id, symbol)
                    refreshed_status = str(refreshed_order.get("status") or "").lower()
                    order_status = refreshed_status or order_status
                    refreshed_filled = safe_float(refreshed_order.get("filled") or actual_filled_qty)
                    if refreshed_filled > actual_filled_qty:
                        actual_filled_qty = min(refreshed_filled, submitted_qty)
                        order = refreshed_order
                    partial_fill_cancel_confirmed = (
                        refreshed_status in {"canceled", "cancelled", "closed", "filled", "rejected", "expired"}
                    )
                    is_partial_fill = (
                        actual_filled_qty > 0
                        and actual_filled_qty < submitted_qty
                    )
                except Exception as refresh_err:
                    logger.error(
                        f"[Exchange] Could not verify partially-filled entry {order_id} after "
                        f"cancellation failure: {refresh_err}"
                    )

            # If the entry remainder is still live or ambiguous, protect the
            # maximum possible exposure. Conditional orders are reduce-only,
            # so this cannot create a reverse position.
            protection_qty = (
                actual_filled_qty
                if partial_fill_cancel_confirmed
                else submitted_qty
            )

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
                    and actual_filled_qty < submitted_qty
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
            if limits and limits.get("contract_size", 1.0) != 1.0:
                contract_size = float(limits.get("contract_size", 1.0))
        except Exception:
            contract_size = 1.0

        result = {
            "status": result_status,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "symbol": symbol,
            "side": side,
            "quantity": actual_filled_qty if actual_filled_qty > 0 else submitted_qty,
            "requested_quantity": requested_qty,
            "submitted_quantity": submitted_qty,
            "entry_price": actual_avg_price if actual_avg_price > 0 else decision.entry_price,
            "sandbox_mode": sandbox_mode,
            "order_type": order_type,
            "limit_timeout_secs": decision.limit_timeout_secs,
            "exchange_order_status": order_status,
            "filled_quantity": actual_filled_qty,
            "is_partial_fill": is_partial_fill,
            "stop_loss": decision.stop_loss,
            "take_profit": decision.take_profit,
            "take_profit_orders": _decision_take_profit_plan(decision),
            # Notional value for correct margin calculation (handles contract markets)
            # Prefer exchange-reported cost, fallback to calculated notional with contract size
            "notional_value": safe_float(order.get("cost"))
            or ((actual_filled_qty if actual_filled_qty > 0 else submitted_qty) * actual_avg_price * contract_size),
            "contract_size": contract_size,
        }
        if partial_fill_cancel_result is not None:
            result["partial_fill_cancel_result"] = partial_fill_cancel_result
        if is_partial_fill and not partial_fill_cancel_confirmed:
            result["entry_remainder_cancel_confirmed"] = False
            result["requires_reconciliation"] = True
            result["warning"] = (
                "Partially-filled entry remainder could not be confirmed cancelled; "
                "protective orders cover the full submitted quantity"
            )
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

        tp_qty = protection_qty if protection_qty > 0 else decision.quantity
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
                tp_coid = f"qp_tp_{uuid.uuid4().hex[:8]}_{int(time.time())}"
                tp_order = await _create_conditional_order(
                    exchange,
                    symbol,
                    "take_profit",
                    tp_side,
                    tp_qty,
                    decision.take_profit,
                    pos_side_for_orders,
                    client_order_id=tp_coid,
                )
                tp_order_id = str(tp_order.get("id") or "").strip()
                if not tp_order_id:
                    raise RuntimeError("Exchange did not return a take-profit order id")
                result["take_profit_order_id"] = tp_order_id
                result["take_profit_protected_qty"] = tp_qty
                logger.info(f"[Exchange] ✅ Take-profit set at {decision.take_profit} (qty={tp_qty})")
            except ccxt.NetworkError as e:
                logger.error(f"[Exchange] Take-profit submission is ambiguous: {e}")
                result["take_profit_error"] = str(e)
                result["take_profit_ambiguous"] = True
            except Exception as e:
                logger.error(f"[Exchange] Failed to set take-profit: {e}")
                result["take_profit_error"] = str(e)

        # ── Stop-Loss / Trailing Stop ──
        trailing_mode = decision.trailing_stop.mode if decision.trailing_stop else TrailingStopMode.NONE
        sl_qty = protection_qty if protection_qty > 0 else decision.quantity

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
                        "reduceOnly": True,
                    },
                    position_side=pos_side_for_orders,
                    client_order_id=f"qp_ts_{uuid.uuid4().hex[:8]}_{int(time.time())}",
                    reduce_only=True,
                )
                trailing_order_id = str(ts_order.get("id") or "").strip()
                if not trailing_order_id:
                    raise RuntimeError("Exchange did not return a trailing-stop order id")
                result["trailing_stop_order_id"] = trailing_order_id
                result["trailing_stop_protected_qty"] = sl_qty
                result["trailing_stop_mode"] = "moving"
                result["trailing_pct"] = trail_pct
                logger.info(f"[Exchange] ✅ Moving trailing stop set: {trail_pct}% (qty={sl_qty})")
            except ccxt.NetworkError as e:
                logger.error(f"[Exchange] Trailing-stop submission is ambiguous: {e}")
                result["trailing_stop_error"] = str(e)
                result["trailing_stop_ambiguous"] = True
                # Never submit a fallback after an ambiguous create call.
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
                entry_remainder_unresolved = is_partial_fill and not partial_fill_cancel_confirmed
                if is_partial_fill and result.get("order_id"):
                    try:
                        cancel_result = await _cancel_exchange_order(exchange, symbol, str(result.get("order_id")))
                        if cancel_result.get("status") == "cancelled":
                            entry_remainder_unresolved = False
                            logger.info(f"[Exchange] Cancelled unfilled entry portion: {cancel_result}")
                        else:
                            logger.error(
                                f"[Exchange] Entry remainder cancellation is still unconfirmed: {cancel_result}"
                            )
                    except Exception as cancel_err:
                        logger.error(f"[Exchange] Failed to cancel unfilled entry portion: {cancel_err}")

                protective_order_ids: list[str] = []
                if result.get("stop_loss_order_id"):
                    protective_order_ids.append(str(result.get("stop_loss_order_id")))
                if result.get("take_profit_order_id"):
                    protective_order_ids.append(str(result.get("take_profit_order_id")))
                for tp_order in result.get("take_profit_orders") or []:
                    if isinstance(tp_order, dict) and tp_order.get("order_id"):
                        protective_order_ids.append(str(tp_order.get("order_id")))
                rollback_cancel_results = []
                protective_cleanup_unresolved = False
                for protective_order_id in dict.fromkeys(protective_order_ids):
                    cancel_result = await _cancel_exchange_order(exchange, symbol, protective_order_id)
                    rollback_cancel_results.append(cancel_result)
                    if cancel_result.get("status") not in _CONFIRMED_CANCEL_STATUSES:
                        protective_cleanup_unresolved = True
                        logger.error(
                            f"[Exchange] Failed to cancel stale protective order {protective_order_id}: {cancel_result}"
                        )
                protection_submission_ambiguous = bool(
                    result.get("stop_loss_ambiguous")
                    or result.get("take_profit_ambiguous")
                    or result.get("trailing_stop_ambiguous")
                    or any(bool(item.get("ambiguous")) for item in (result.get("take_profit_orders") or []))
                )

                logger.warning(
                    f"[Exchange] Protection orders failed for filled entry. "
                    f"Closing position {symbol} for safety. Errors: {protection_errors}"
                )
                # P0-FIX: Retry rollback close using retry-capable _close_position
                try:
                    close_result = await _close_position(
                        exchange, symbol, position_side=pos_side_for_orders, close_quantity=actual_filled_qty, max_retries=3
                    )
                    if close_result.get("status") == "closed":
                        if entry_remainder_unresolved or protection_submission_ambiguous or protective_cleanup_unresolved:
                            unresolved_reasons = []
                            if entry_remainder_unresolved:
                                unresolved_reasons.append("entry remainder cancellation was not confirmed")
                            if protection_submission_ambiguous:
                                unresolved_reasons.append("a protective order submission timed out")
                            if protective_cleanup_unresolved:
                                unresolved_reasons.append("known protective order cancellation was not confirmed")
                            return {
                                "status": "manual_review",
                                "reason": (
                                    "Filled exposure was closed after protection failure, but "
                                    + "; ".join(unresolved_reasons)
                                ),
                                "entry_order_id": result.get("order_id"),
                                "close_order_id": close_result.get("order_id"),
                                "exit_price": close_result.get("exit_price"),
                                "protection_errors": protection_errors,
                                "protective_cancel_results": rollback_cancel_results,
                                "current_exposure_closed": True,
                                "rollback_success": False,
                                "requires_reconciliation": True,
                            }
                        return {
                            "status": "error",
                            "reason": "Entry filled but protection failed - position closed for safety",
                            "entry_order_id": result.get("order_id"),
                            "close_order_id": close_result.get("order_id"),
                            "exit_price": close_result.get("exit_price"),
                            "protection_errors": protection_errors,
                            "protective_cancel_results": rollback_cancel_results,
                            "rollback_success": True,
                        }
                    else:
                        logger.error(f"[Exchange] CRITICAL: Failed to rollback unprotected position: {close_result}")
                        result["status"] = "partial_protection"
                        result["protection_errors"] = protection_errors
                        result["protective_cancel_results"] = rollback_cancel_results
                        result["warning"] = "CRITICAL: Position opened but SL/TP failed - MANUAL STOP LOSS REQUIRED"
                        return result
                except ccxt.BaseError as rollback_err:
                    logger.error(f"[Exchange] CRITICAL: Rollback exception: {rollback_err}")
                    result["status"] = "partial_protection"
                    result["protection_errors"] = protection_errors
                    result["protective_cancel_results"] = rollback_cancel_results
                    result["warning"] = "CRITICAL: Rollback failed - MANUAL STOP LOSS REQUIRED"
                    return result
                except Exception as rollback_err:
                    logger.error(f"[Exchange] CRITICAL: Unexpected rollback exception: {rollback_err}")
                    result["status"] = "partial_protection"
                    result["protection_errors"] = protection_errors
                    result["protective_cancel_results"] = rollback_cancel_results
                    result["warning"] = "CRITICAL: Rollback failed - MANUAL STOP LOSS REQUIRED"
                    return result
            else:
                # Entry pending - cancel order and return error
                pending_cancel_confirmed = False
                if result.get("order_id"):
                    try:
                        cancel_result = await _cancel_exchange_order(exchange, symbol, str(result.get("order_id")))
                        pending_cancel_confirmed = cancel_result.get("status") == "cancelled"
                        logger.warning(f"[Exchange] Cancelled pending entry {result.get('order_id')} after protection failure: {cancel_result}")
                    except Exception as cancel_err:
                        logger.error(f"[Exchange] Failed to cancel pending entry after protection failure: {cancel_err}")
                logger.warning("[Exchange] Protection failed for pending entry, order cancelled")
                result["protection_errors"] = protection_errors
                if pending_cancel_confirmed:
                    result["warning"] = "Protection orders failed - pending entry cancelled"
                else:
                    result.update({
                        "status": "manual_review",
                        "warning": "Protection failed and pending entry cancellation was not confirmed",
                        "requires_reconciliation": True,
                        "retry_safe": False,
                    })

        return result

    except ccxt.InsufficientFunds as e:
        logger.error(f"[Exchange] Insufficient funds: {e}")
        return {
            "status": "error",
            "reason": f"Insufficient funds: {e}",
            "client_order_id": client_order_id,
        }
    except OrderValidationError as e:
        logger.error(f"[Exchange] Order validation blocked submission: {e}")
        return {
            "status": "error",
            "reason": str(e),
            "client_order_id": client_order_id,
            "failure_stage": "pre_execution",
        }
    except ccxt.NetworkError as e:
        logger.error(f"[Exchange] Network error: {e}")
        return {
            "status": "error",
            "reason": f"Network error: {e}",
            "client_order_id": client_order_id,
            "requires_reconciliation": bool(order_submission_started),
        }
    except ccxt.BaseError as e:
        logger.error(f"[Exchange] Exchange error: {e}")
        return {
            "status": "error",
            "reason": f"Exchange error: {e}",
            "client_order_id": client_order_id,
            "requires_reconciliation": bool(order_submission_started),
        }
    except Exception as e:
        logger.error(f"[Exchange] Order failed: {e}")
        return {
            "status": "error",
            "reason": f"Order execution failed: {e}",
            "client_order_id": client_order_id,
            "requires_reconciliation": bool(order_submission_started),
        }


async def _place_stop_loss(exchange, symbol, side, quantity, stop_price, result, position_side: str | None = None):
    """Place a standard stop-loss order.

    Args:
        side: The entry order side (buy for long, sell for short)
        position_side: For OKX hedge mode, the position being protected.
    """
    try:
        sl_side = "sell" if side == "buy" else "buy"
        pos_side = position_side or ("long" if side == "buy" else "short")
        sl_coid = f"qp_sl_{uuid.uuid4().hex[:8]}_{int(time.time())}"
        sl_order = await _create_conditional_order(exchange, symbol, "stop_loss", sl_side, quantity, stop_price, pos_side, client_order_id=sl_coid)
        stop_order_id = str(sl_order.get("id") or "").strip()
        if not stop_order_id:
            raise RuntimeError("Exchange did not return a stop-loss order id")
        result["stop_loss_order_id"] = stop_order_id
        result["stop_loss_protected_qty"] = quantity
        logger.info(f"[Exchange] ✅ Stop-loss set at {stop_price} (qty={quantity}, position_side={pos_side})")
    except ccxt.NetworkError as e:
        logger.error(f"[Exchange] Stop-loss submission is ambiguous: {e}")
        result["stop_loss_error"] = "Stop-loss submission timed out"
        result["stop_loss_ambiguous"] = True
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
            tp_coid = f"qp_tp{i+1}_{uuid.uuid4().hex[:8]}_{int(time.time())}"
            tp_order = await _create_conditional_order(
                exchange, symbol, "take_profit", tp_side, round(tp_qty, 6), tp.price, pos_side, client_order_id=tp_coid
            )
            tp_order_id = str(tp_order.get("id") or "").strip()
            if not tp_order_id:
                raise RuntimeError(f"Exchange did not return an id for TP{i+1}")
            tp_results.append({
                "level": i + 1,
                "price": tp.price,
                "qty": round(tp_qty, 6),
                "qty_pct": qty_pct,
                "order_id": tp_order_id,
                "status": "placed",
                "position_side": pos_side,
            })
            logger.info(f"[Exchange] ✅ TP{i+1} set at {tp.price} ({qty_pct}% = {tp_qty}, position_side={pos_side})")
        except ccxt.NetworkError as e:
            logger.error(f"[Exchange] TP{i+1} submission is ambiguous: {e}")
            tp_results.append({
                "level": i + 1,
                "price": tp.price,
                "qty": round(tp_qty, 6),
                "qty_pct": qty_pct,
                "error": "Take-profit submission timed out",
                "status": "failed",
                "ambiguous": True,
            })
            # Do not submit more protection after an ambiguous create call.
            break
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


def _conditional_order_attempts(exchange_id: str, kind: str, trigger_price: float, position_side: str | None = None, margin_mode: str = "cross") -> list[tuple[str, dict[str, Any]]]:
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
        candidates.insert(0, ("market", {**reduce_params, key: trigger_price, order_key: "-1", "tdMode": margin_mode}))
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


async def _create_conditional_order(exchange, symbol: str, kind: str, side: str, amount: float, trigger_price: float, position_side: str | None = None, client_order_id: str | None = None) -> dict:
    """Try exchange-specific conditional order formats before failing.

    Args:
        position_side: For OKX hedge mode, the position being protected ('long' or 'short').
                       For LONG position TP/SL: side=sell, position_side=long
                       For SHORT position TP/SL: side=buy, position_side=short
                       For Bybit, determines triggerDirection.
    """
    exchange_id = _exchange_id(exchange)
    exchange_options = getattr(exchange, "options", {}) or {}
    effective_margin_mode = str(exchange_options.get("defaultMarginMode") or settings.risk.margin_mode or "cross").lower()
    if not client_order_id:
        client_order_id = client_order_id_for_idempotency(
            f"protective:{exchange_id}:{symbol}:{kind}:{side}:{float(amount):.12g}:"
            f"{float(trigger_price):.12g}:{position_side or ''}"
        )
    errors = []
    for order_type, params in _conditional_order_attempts(exchange_id, kind, trigger_price, position_side, margin_mode=effective_margin_mode):
        try:
            return await _create_exchange_order(
                exchange,
                symbol=symbol,
                order_type=order_type,
                side=side,
                amount=amount,
                params=params,
                position_side=position_side,
                allow_amount_increase=False,
                client_order_id=client_order_id,
            )
        except ccxt.NetworkError as exc:
            from core.reconciliation_journal import record_reconciliation_issue

            record_reconciliation_issue(
                ticker=symbol,
                symbol=symbol,
                exchange=exchange_id,
                order_ids=[],
                operation=f"ambiguous_{kind}_submission",
                reason=str(exc),
                context={"client_order_id": client_order_id, "side": side, "amount": amount, "trigger_price": trigger_price},
            )
            raise
        except ccxt.BaseError as exc:
            errors.append(f"{order_type}: {exc}")
            logger.debug(f"[Exchange] {exchange_id} {kind} candidate failed: {order_type} {exc}")
        except Exception as exc:
            errors.append(f"{order_type}: {exc}")
            logger.debug(f"[Exchange] {exchange_id} {kind} candidate failed: {order_type} {exc}")
    raise RuntimeError("; ".join(errors[-3:]) or f"Failed to create {kind} order")


_CONFIRMED_CANCEL_STATUSES = {"cancelled", "canceled", "not_found", "simulated"}


def _record_cancel_reconciliation(
    *,
    ticker: str,
    order_ids: list[str],
    operation: str,
    reason: str,
    symbol: str = "",
    exchange=None,
    context: dict | None = None,
) -> str:
    from core.reconciliation_journal import record_reconciliation_issue

    return record_reconciliation_issue(
        ticker=ticker,
        symbol=symbol,
        exchange=_exchange_id(exchange) if exchange is not None else "",
        order_ids=order_ids,
        operation=operation,
        reason=reason,
        context=context,
    )


async def _cancel_exchange_order(
    exchange,
    symbol: str,
    order_id: str,
    *,
    max_attempts: int = 3,
) -> dict:
    """Cancel an exchange order and retry transient/ambiguous failures."""
    if not order_id:
        return {"status": "skipped", "order_id": "", "symbol": symbol}

    attempts = max(1, int(max_attempts or 1))
    last_error = ""
    for attempt in range(1, attempts + 1):
        try:
            result = await asyncio.to_thread(exchange.cancel_order, order_id, symbol)
            return {
                "status": "cancelled",
                "order_id": str((result or {}).get("id") or order_id),
                "symbol": symbol,
                "attempts": attempt,
            }
        except ccxt.OrderNotFound:
            return {"status": "not_found", "order_id": order_id, "symbol": symbol}
        except Exception as exc:
            if _is_order_not_found_error(exc):
                return {"status": "not_found", "order_id": order_id, "symbol": symbol}
            last_error = str(exc)
            if attempt < attempts:
                logger.warning(
                    f"[Exchange] Cancel attempt {attempt}/{attempts} failed for "
                    f"{order_id} on {symbol}: {exc}"
                )
                await asyncio.sleep(min(0.25 * (2 ** (attempt - 1)), 1.0))
                continue
            logger.error(f"[Exchange] Failed to cancel order {order_id} on {symbol}: {exc}")

    return {
        "status": "error",
        "order_id": order_id,
        "symbol": symbol,
        "reason": last_error or "Cancellation could not be confirmed",
        "attempts": attempts,
    }


async def cancel_order(order_id: str, ticker: str, exchange_config: dict | None = None) -> dict:
    """Cancel a specific exchange order."""
    exchange_config = exchange_config or {}
    if not order_id:
        return {"status": "skipped", "order_id": "", "ticker": ticker}
    if not safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False):
        return {"status": "simulated", "order_id": order_id, "ticker": ticker}

    exchange = None
    try:
        exchange = _get_or_create_exchange(
            exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
            api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
            api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
            password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
            live=True,
            sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
            market_type=exchange_config.get("market_type") or settings.exchange.market_type,
            margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
        )
        symbol = await asyncio.to_thread(
            _resolve_symbol,
            exchange,
            ticker,
            exchange_config.get("market_type") or settings.exchange.market_type,
        )
        result = await _cancel_exchange_order(exchange, symbol, order_id)
        if result.get("status") not in _CONFIRMED_CANCEL_STATUSES:
            result["reconciliation_id"] = _record_cancel_reconciliation(
                ticker=ticker,
                symbol=symbol,
                exchange=exchange,
                order_ids=[order_id],
                operation="cancel_order",
                reason=str(result.get("reason") or result.get("status") or "cancel failed"),
            )
        return result
    except ccxt.BaseError as exc:
        logger.error(f"[Exchange] Failed to cancel order {order_id} for {ticker}: {exc}")
        result = {"status": "error", "order_id": order_id, "ticker": ticker, "reason": str(exc)}
    except Exception as exc:
        logger.error(f"[Exchange] Unexpected error cancelling order {order_id} for {ticker}: {exc}")
        result = {"status": "error", "order_id": order_id, "ticker": ticker, "reason": str(exc)}
    result["reconciliation_id"] = _record_cancel_reconciliation(
        ticker=ticker,
        exchange=exchange,
        order_ids=[order_id],
        operation="cancel_order",
        reason=str(result.get("reason") or "cancel failed"),
    )
    return result


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
    if not safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False):
        return {"status": "simulated", "stop_price": stop_price}
    exchange = None
    symbol = ""
    protection_submission_started = False
    try:
        exchange = _get_or_create_exchange(
            exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
            api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
            api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
            password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
            live=True,
            sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
            market_type=exchange_config.get("market_type") or settings.exchange.market_type,
            margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
        )
        symbol = await asyncio.to_thread(
            _resolve_symbol,
            exchange,
            ticker,
            exchange_config.get("market_type") or settings.exchange.market_type,
        )
        side = "sell" if str(direction).lower() == SignalDirection.LONG.value else "buy"
        pos_side_for_sl = "long" if str(direction).lower() in ("long", SignalDirection.LONG.value) else "short"
        # P0-FIX: Create new SL first, then cancel old SL to prevent naked exposure.
        protection_submission_started = True
        order = await _create_conditional_order(exchange, symbol, "stop_loss", side, quantity, stop_price, pos_side_for_sl)
        new_order_id = order.get("id")
        if not new_order_id:
            raise RuntimeError("New protective stop created but returned no order ID")
        cancel_result = {}
        if existing_order_id:
            cancel_result = await _cancel_exchange_order(exchange, symbol, str(existing_order_id))
            if cancel_result.get("status") not in _CONFIRMED_CANCEL_STATUSES:
                rollback_result = await _cancel_exchange_order(exchange, symbol, str(new_order_id))
                reason = (
                    f"Old protective stop {existing_order_id} could not be cancelled after "
                    f"new stop {new_order_id} was placed"
                )
                if rollback_result.get("status") in _CONFIRMED_CANCEL_STATUSES:
                    logger.error(f"[Exchange] {reason}; new stop rolled back, old stop retained")
                    return {
                        "status": "error",
                        "reason": reason,
                        "existing_order_id": str(existing_order_id),
                        "replace_cancel_result": cancel_result,
                        "rollback_cancel_result": rollback_result,
                    }
                reconciliation_id = _record_cancel_reconciliation(
                    ticker=ticker,
                    symbol=symbol,
                    exchange=exchange,
                    order_ids=[str(existing_order_id), str(new_order_id)],
                    operation="replace_protective_stop",
                    reason=reason,
                    context={
                        "old_cancel": cancel_result,
                        "new_rollback_cancel": rollback_result,
                        "stop_price": stop_price,
                    },
                )
                logger.critical(f"[Exchange] {reason}; both orders require reconciliation ({reconciliation_id})")
                return {
                    "status": "manual_review",
                    "reason": reason,
                    "active_order_ids": [str(existing_order_id), str(new_order_id)],
                    "existing_order_id": str(existing_order_id),
                    "new_order_id": str(new_order_id),
                    "replace_cancel_result": cancel_result,
                    "rollback_cancel_result": rollback_result,
                    "reconciliation_id": reconciliation_id,
                }
            logger.info(f"[Exchange] Old protective stop {existing_order_id} cancelled after new stop {new_order_id} placed")
        result = {"status": "placed", "order_id": new_order_id, "symbol": symbol, "stop_price": stop_price, "position_side": pos_side_for_sl}
        if existing_order_id:
            result["replace_cancel_result"] = cancel_result
            result["replaced_order_id"] = str(cancel_result.get("order_id") or existing_order_id)
        return result
    except ccxt.NetworkError as e:
        logger.critical(f"[Exchange] Protective stop submission is ambiguous: {e}")
        close_result: dict[str, Any] = {}
        if protection_submission_started and exchange is not None and symbol:
            try:
                close_result = await _close_position(
                    exchange,
                    symbol,
                    position_side="long" if str(direction).lower() == SignalDirection.LONG.value else "short",
                    close_quantity=quantity,
                    max_retries=3,
                )
            except Exception as close_exc:
                close_result = {"status": "error", "reason": str(close_exc)}
        return {
            "status": "manual_review" if protection_submission_started else "error",
            "reason": str(e),
            "requires_reconciliation": protection_submission_started,
            "current_exposure_closed": str(close_result.get("status") or "").lower() == "closed",
            "close_result": close_result,
        }
    except ccxt.BaseError as e:
        logger.error(f"[Exchange] Failed to place protective stop: {e}")
        return {"status": "error", "reason": str(e)}
    except Exception as e:
        logger.error(f"[Exchange] Unexpected error placing protective stop: {e}")
        return {"status": "error", "reason": str(e)}


async def place_protective_take_profit(
    ticker: str,
    direction: str,
    quantity: float,
    take_profit_price: float,
    exchange_config: dict | None = None,
    existing_order_id: str | None = None,
) -> dict:
    """Place a reduce-only take-profit and replace the previous order safely."""
    exchange_config = exchange_config or {}
    if not safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False):
        return {"status": "simulated", "take_profit_price": take_profit_price}
    exchange = None
    symbol = ""
    protection_submission_started = False
    try:
        exchange = _get_or_create_exchange(
            exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
            api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
            api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
            password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
            live=True,
            sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
            market_type=exchange_config.get("market_type") or settings.exchange.market_type,
            margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
        )
        symbol = await asyncio.to_thread(
            _resolve_symbol,
            exchange,
            ticker,
            exchange_config.get("market_type") or settings.exchange.market_type,
        )
        normalized_direction = str(direction).lower()
        side = "sell" if normalized_direction == SignalDirection.LONG.value else "buy"
        position_side = "long" if normalized_direction in ("long", SignalDirection.LONG.value) else "short"
        protection_submission_started = True
        order = await _create_conditional_order(
            exchange,
            symbol,
            "take_profit",
            side,
            quantity,
            take_profit_price,
            position_side,
        )
        new_order_id = order.get("id")
        if not new_order_id:
            raise RuntimeError("New protective take-profit created but returned no order ID")

        cancel_result = {}
        if existing_order_id:
            cancel_result = await _cancel_exchange_order(exchange, symbol, str(existing_order_id))
            if cancel_result.get("status") not in _CONFIRMED_CANCEL_STATUSES:
                rollback_result = await _cancel_exchange_order(exchange, symbol, str(new_order_id))
                reason = (
                    f"Old protective take-profit {existing_order_id} could not be cancelled "
                    f"after new take-profit {new_order_id} was placed"
                )
                if rollback_result.get("status") in _CONFIRMED_CANCEL_STATUSES:
                    logger.error(f"[Exchange] {reason}; new take-profit rolled back, old order retained")
                    return {
                        "status": "error",
                        "reason": reason,
                        "existing_order_id": str(existing_order_id),
                        "replace_cancel_result": cancel_result,
                        "rollback_cancel_result": rollback_result,
                    }
                reconciliation_id = _record_cancel_reconciliation(
                    ticker=ticker,
                    symbol=symbol,
                    exchange=exchange,
                    order_ids=[str(existing_order_id), str(new_order_id)],
                    operation="replace_protective_take_profit",
                    reason=reason,
                    context={
                        "old_cancel": cancel_result,
                        "new_rollback_cancel": rollback_result,
                        "take_profit_price": take_profit_price,
                    },
                )
                logger.critical(f"[Exchange] {reason}; both orders require reconciliation ({reconciliation_id})")
                return {
                    "status": "manual_review",
                    "reason": reason,
                    "active_order_ids": [str(existing_order_id), str(new_order_id)],
                    "existing_order_id": str(existing_order_id),
                    "new_order_id": str(new_order_id),
                    "replace_cancel_result": cancel_result,
                    "rollback_cancel_result": rollback_result,
                    "reconciliation_id": reconciliation_id,
                }
            logger.info(
                f"[Exchange] Replaced protective take-profit {existing_order_id} "
                f"with {new_order_id}: cancel_status={cancel_result.get('status')}"
            )

        result = {
            "status": "placed",
            "order_id": new_order_id,
            "symbol": symbol,
            "take_profit_price": take_profit_price,
            "position_side": position_side,
        }
        if existing_order_id:
            result["replace_cancel_result"] = cancel_result
            result["replaced_order_id"] = str(cancel_result.get("order_id") or existing_order_id)
        return result
    except ccxt.NetworkError as exc:
        logger.critical(f"[Exchange] Protective take-profit submission is ambiguous: {exc}")
        return {
            "status": "manual_review" if protection_submission_started else "error",
            "reason": str(exc),
            "requires_reconciliation": protection_submission_started,
        }
    except ccxt.BaseError as exc:
        logger.error(f"[Exchange] Failed to place protective take-profit: {exc}")
        return {"status": "error", "reason": str(exc)}
    except Exception as exc:
        logger.error(f"[Exchange] Unexpected error placing protective take-profit: {exc}")
        return {"status": "error", "reason": str(exc)}


def _normalized_position_side(position: dict, contracts: float | None = None) -> str:
    side = str(position.get("side") or "").lower().strip()
    if not side:
        info = position.get("info") or {}
        if isinstance(info, dict):
            side = str(info.get("posSide") or info.get("positionSide") or "").lower().strip()
    if side in {"buy", "long"}:
        return "long"
    if side in {"sell", "short"}:
        return "short"
    if contracts is not None and contracts != 0:
        return "long" if contracts > 0 else "short"
    return side


def _position_symbol_matches(symbol: str, position: dict) -> bool:
    position_symbol = str(position.get("symbol") or "")
    if position_symbol == symbol:
        return True
    try:
        from core.utils.common import position_symbol_key
        return position_symbol_key(position_symbol) == position_symbol_key(symbol)
    except Exception:
        return False


def _position_side_matches(requested_side: str | None, actual_side: str) -> bool:
    requested = str(requested_side or "").lower().strip()
    if requested in {"buy", "long"}:
        requested = "long"
    elif requested in {"sell", "short"}:
        requested = "short"
    if not requested:
        return True
    if not actual_side:
        return False
    return requested == actual_side or requested in actual_side or actual_side in requested


async def _fetch_matching_exchange_position(
    exchange: ccxt.Exchange,
    symbol: str,
    position_side: str | None = None,
) -> tuple[dict | None, float, str]:
    positions = await asyncio.to_thread(exchange.fetch_positions, [symbol])
    for pos in positions:
        if not _position_symbol_matches(symbol, pos):
            continue
        contracts_raw = safe_float(pos.get("contracts") or 0)
        if contracts_raw == 0:
            continue
        pos_side = _normalized_position_side(pos, contracts_raw)
        if not _position_side_matches(position_side, pos_side):
            continue
        return pos, abs(contracts_raw), pos_side
    return None, 0.0, ""


async def _verify_position_close(
    exchange: ccxt.Exchange,
    symbol: str,
    position_side: str | None,
    *,
    attempts: int = _CLOSE_VERIFY_ATTEMPTS,
    delay_secs: float = _CLOSE_VERIFY_DELAY_SECS,
) -> dict:
    """Confirm a close by re-reading exchange positions.

    A reduce-only market order being accepted is not enough to mark the
    position closed; the exchange position must actually disappear or reach
    zero contracts.
    """
    last_position: dict | None = None
    last_contracts = 0.0
    last_side = ""
    consecutive_flat_reads = 0
    for attempt in range(max(1, attempts)):
        match, contracts, side = await _fetch_matching_exchange_position(exchange, symbol, position_side)
        last_position, last_contracts, last_side = match, contracts, side
        if match is None or contracts <= _CLOSE_FLAT_CONTRACT_EPSILON:
            consecutive_flat_reads += 1
            # A single empty response is not sufficient proof of a flat
            # account. Require two independent successful reads.
            if consecutive_flat_reads >= 2:
                return {
                    "flat": True,
                    "remaining_contracts": 0.0,
                    "position_side": side or position_side,
                    "attempts": attempt + 1,
                    "consecutive_flat_reads": consecutive_flat_reads,
                }
        else:
            consecutive_flat_reads = 0
        if attempt < attempts - 1:
            await asyncio.sleep(delay_secs)
    return {
        "flat": False,
        "remaining_contracts": last_contracts,
        "position_side": last_side or position_side,
        "position": last_position,
        "attempts": attempts,
    }


async def _close_position(
    exchange: ccxt.Exchange,
    symbol: str,
    position_side: str | None = None,
    close_quantity: float | None = None,
    max_retries: int = 3,
    client_order_id: str | None = None,
) -> dict:
    """Close an existing position with retry logic.

    Args:
        position_side: For hedge mode exchanges (OKX), specify 'long' or 'short'.
                       If None, closes first found position (may be wrong in hedge mode).
        close_quantity: If specified, only close this quantity (for partial rollback).
                        If None, close entire position.
        max_retries: Maximum reduce-only close attempts for full closes; also caps transient retry attempts.
    """
    last_error = None
    last_unconfirmed: dict | None = None
    close_order_ids: list[str] = []
    base_close_coid = str(client_order_id or f"qp_close_{uuid.uuid4().hex[:16]}")[:96]
    next_close_coid = base_close_coid
    residual_sequence = 1
    for attempt in range(1, max_retries + 1):
        try:
            positions = await asyncio.to_thread(exchange.fetch_positions, [symbol])
            found_matching_position = False
            retry_unconfirmed_close = False
            for pos in positions:
                if not _position_symbol_matches(symbol, pos):
                    continue
                contracts = float(pos.get("contracts", 0))
                if contracts == 0:
                    continue

                pos_side = _normalized_position_side(pos, contracts)
                if position_side and not _position_side_matches(position_side, pos_side):
                    continue
                found_matching_position = True

                amount = abs(contracts)
                requested_full_close = not close_quantity or close_quantity >= (amount - _CLOSE_FLAT_CONTRACT_EPSILON)
                if close_quantity and close_quantity > 0:
                    amount = min(amount, close_quantity)
                close_side = "sell" if pos_side == "long" else "buy"

                # FIX (Round-4 audit P0): previously on attempt > 1 we appended
                # `_{attempt}` to client_order_id. This BROKE idempotency: if the
                # first close attempt actually succeeded on the exchange but the
                # client saw a network timeout, the second attempt would use a
                # different coid and place a SECOND close order — which, on
                # hedge-mode exchanges, could open a position in the opposite
                # direction. Fix: always use the SAME client_order_id. If the
                # exchange already has an order with that coid, it will reject
                # the duplicate (which we then treat as success by fetching the
                # existing order).
                effective_coid = next_close_coid

                # Before re-sending, check if a previous attempt with this coid
                # already landed on the exchange (network error recovery).
                if attempt > 1 and effective_coid:
                    try:
                        existing_orders = await asyncio.to_thread(
                            exchange.fetch_orders, symbol, None, 5
                        )
                        already_placed = None
                        for o in (existing_orders or []):
                            if str(o.get("clientOrderId") or "") == effective_coid:
                                already_placed = o
                                break
                        if already_placed:
                            existing_order_id = str(already_placed.get("id") or "")
                            if existing_order_id and existing_order_id not in close_order_ids:
                                close_order_ids.append(existing_order_id)
                            logger.info(
                                f"[Exchange] Close order with coid={effective_coid} already exists "
                                f"(id={already_placed.get('id')}, status={already_placed.get('status')}). "
                                f"Treating as placed, verifying position flat."
                            )
                            verify = await _verify_position_close(exchange, symbol, pos_side or position_side)
                            if verify.get("flat"):
                                return {
                                    "status": "closed",
                                    "order_id": already_placed.get("id"),
                                    "client_order_id": effective_coid,
                                    "exit_price": already_placed.get("average") or already_placed.get("price"),
                                    "closed_quantity": safe_float(already_placed.get("filled") or amount),
                                    "position_side": pos_side,
                                    "remaining_contracts": 0.0,
                                    "close_verification": verify,
                                    "close_attempts": attempt,
                                    "reused_existing_order": True,
                                }
                            existing_status = str(already_placed.get("status") or "").lower()
                            if existing_status in {"open", "new", "partially_filled", "pending"}:
                                last_unconfirmed = {
                                    "status": "close_unconfirmed",
                                    "reason": (
                                        f"Existing reduce-only close order {existing_order_id or effective_coid} "
                                        f"is still {existing_status}; position remains open"
                                    ),
                                    "order_id": existing_order_id or None,
                                    "client_order_id": effective_coid,
                                    "position_side": pos_side,
                                    "remaining_contracts": safe_float(verify.get("remaining_contracts") or amount),
                                    "close_verification": verify,
                                    "close_attempts": attempt,
                                    "requires_reconciliation": True,
                                }
                                if close_order_ids:
                                    last_unconfirmed["close_order_ids"] = close_order_ids
                                if attempt < max_retries:
                                    await asyncio.sleep(min(0.5 * attempt, 2.0))
                                    retry_unconfirmed_close = True
                                    break
                                return last_unconfirmed

                            if existing_status not in {
                                "closed",
                                "filled",
                                "canceled",
                                "cancelled",
                                "rejected",
                                "expired",
                            }:
                                return {
                                    "status": "close_unconfirmed",
                                    "reason": (
                                        f"Existing close order {existing_order_id or effective_coid} has "
                                        f"unknown status '{existing_status}' while the position remains open"
                                    ),
                                    "order_id": existing_order_id or None,
                                    "client_order_id": effective_coid,
                                    "remaining_contracts": safe_float(verify.get("remaining_contracts") or amount),
                                    "close_verification": verify,
                                    "requires_reconciliation": True,
                                }

                            # A terminal close order may have only partially
                            # filled. A new reduce-only order is required for
                            # the residual, with its own deterministic id.
                            residual_sequence += 1
                            next_close_coid = f"{base_close_coid}_r{residual_sequence}"[:128]
                            effective_coid = next_close_coid
                            logger.warning(
                                f"[Exchange] Terminal close order {existing_order_id or effective_coid} "
                                f"left {verify.get('remaining_contracts')} contracts; sending residual "
                                f"reduce-only close with coid={effective_coid}"
                            )
                    except Exception as lookup_err:
                        logger.debug(f"[Exchange] coid pre-check failed: {lookup_err}")

                order = await _create_exchange_order(
                    exchange,
                    symbol=symbol,
                    order_type="market",
                    side=close_side,
                    amount=amount,
                    params={"reduceOnly": True},
                    position_side=pos_side if pos_side else None,
                    allow_amount_increase=False,
                    client_order_id=effective_coid,
                )
                order_id = str(order.get("id") or "")
                if order_id:
                    close_order_ids.append(order_id)
                actual_filled = safe_float(order.get("filled") or amount)
                verify = await _verify_position_close(exchange, symbol, pos_side or position_side)
                exit_price = order.get("average") or order.get("price") or pos.get("markPrice") or pos.get("entryPrice")
                if verify.get("flat"):
                    logger.info(f"[Exchange] ✅ Position close confirmed flat: {order.get('id')} (side={pos_side or 'net'})")
                    result = {
                        "status": "closed",
                        "order_id": order.get("id"),
                        "client_order_id": effective_coid,
                        "exit_price": exit_price,
                        "closed_quantity": actual_filled,
                        "requested_close_quantity": close_quantity or amount,
                        "position_side": pos_side,
                        "remaining_contracts": 0.0,
                        "close_verification": verify,
                        "close_attempts": attempt,
                    }
                    if close_order_ids:
                        result["close_order_ids"] = close_order_ids
                    return result
                remaining = safe_float(verify.get("remaining_contracts") or 0)
                if not requested_full_close:
                    reason = f"Partial close accepted; exchange still reports {remaining} contracts"
                    logger.warning(f"[Exchange] {reason} for {symbol} side={pos_side}")
                    result = {
                        "status": "partial_closed",
                        "reason": reason,
                        "order_id": order.get("id"),
                        "client_order_id": client_order_id,
                        "exit_price": exit_price,
                        "closed_quantity": actual_filled,
                        "requested_close_quantity": close_quantity or amount,
                        "position_side": pos_side,
                        "remaining_contracts": remaining,
                        "close_verification": verify,
                        "close_attempts": attempt,
                    }
                    if close_order_ids:
                        result["close_order_ids"] = close_order_ids
                    return result

                reason = f"Close order accepted but exchange still reports {remaining} contracts"
                last_unconfirmed = {
                    "status": "close_unconfirmed",
                    "reason": reason,
                    "order_id": order.get("id"),
                    "client_order_id": effective_coid,
                    "exit_price": exit_price,
                    "closed_quantity": actual_filled,
                    "requested_close_quantity": close_quantity or amount,
                    "position_side": pos_side,
                    "remaining_contracts": remaining,
                    "close_verification": verify,
                    "close_attempts": attempt,
                }
                if close_order_ids:
                    last_unconfirmed["close_order_ids"] = close_order_ids
                if attempt < max_retries:
                    delay = min(0.5 * attempt, 2.0)
                    logger.error(
                        f"[Exchange] CRITICAL: {reason} for {symbol} side={pos_side}. "
                        f"Retrying reduce-only close in {delay:.1f}s "
                        f"(attempt {attempt + 1}/{max_retries})"
                    )
                    await asyncio.sleep(delay)
                    retry_unconfirmed_close = True
                    break

                logger.error(
                    f"[Exchange] CRITICAL: {reason} for {symbol} side={pos_side} "
                    f"after {max_retries} close attempts"
                )
                return last_unconfirmed

            if retry_unconfirmed_close:
                continue

            if not found_matching_position and last_unconfirmed:
                retry_verify = await _verify_position_close(
                    exchange,
                    symbol,
                    position_side,
                )
                if not retry_verify.get("flat"):
                    last_unconfirmed["close_verification"] = retry_verify
                    last_unconfirmed["requires_reconciliation"] = True
                    return last_unconfirmed
                logger.info(
                    f"[Exchange] ✅ Position close confirmed flat after repeated retry fetches: "
                    f"{symbol} side={position_side or 'net'}"
                )
                result = {
                    "status": "closed",
                    "order_id": last_unconfirmed.get("order_id"),
                    "exit_price": last_unconfirmed.get("exit_price"),
                    "position_side": last_unconfirmed.get("position_side") or position_side,
                    "remaining_contracts": 0.0,
                    "close_verification": retry_verify,
                    "close_attempts": attempt,
                }
                if close_order_ids:
                    result["close_order_ids"] = close_order_ids
                return result

            if not found_matching_position and attempt < max_retries:
                await asyncio.sleep(min(0.5 * attempt, 2.0))
                continue
            return {"status": "no_position", "reason": f"No open {position_side or ''} position to close"}
        except (ccxt.NetworkError, ccxt.ExchangeNotAvailable, ccxt.RateLimitExceeded, ccxt.RequestTimeout) as e:
            last_error = e
            if attempt < max_retries:
                delay = min(2 ** attempt, 10)
                logger.warning(
                    f"[Exchange] Transient error closing {symbol} (attempt {attempt}/{max_retries}): {e}. "
                    f"Retrying in {delay}s..."
                )
                await asyncio.sleep(delay)
                continue
            logger.error(f"[Exchange] Failed to close {symbol} after {max_retries} retries: {e}")
        except ccxt.BaseError as e:
            logger.error(f"[Exchange] Failed to close position: {e}")
            try:
                verify = await _verify_position_close(exchange, symbol, position_side)
                if verify.get("flat"):
                    return {
                        "status": "closed",
                        "reason": "Exchange rejected a duplicate close request, but two position reads confirmed flat",
                        "client_order_id": next_close_coid,
                        "close_order_ids": close_order_ids,
                        "remaining_contracts": 0.0,
                        "close_verification": verify,
                    }
            except Exception as verify_error:
                verify = {"flat": False, "verification_error": str(verify_error)}
            return {
                "status": "close_unconfirmed",
                "reason": f"Failed to close position: {e}",
                "client_order_id": next_close_coid,
                "close_order_ids": close_order_ids,
                "close_verification": verify,
                "requires_reconciliation": True,
            }
        except Exception as e:
            logger.error(f"[Exchange] Unexpected error closing position: {e}")
            return {"status": "error", "reason": "Failed to close position"}
    if last_unconfirmed:
        last_unconfirmed["requires_reconciliation"] = True
        return last_unconfirmed
    return {
        "status": "close_unconfirmed",
        "reason": f"Failed to close position after {max_retries} retries: {last_error}",
        "client_order_id": next_close_coid,
        "close_order_ids": close_order_ids,
        "requires_reconciliation": True,
    }


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
    if not safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False):
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
        sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
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
    if not safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False):
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
        sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
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
        live=safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False),
        sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
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
            "_data_reliable": True,
        }
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch ticker for {symbol}: {e}")
        return {"_data_reliable": False}


async def get_latest_candle(symbol: str, timeframe: str = "1m", exchange_config: dict | None = None) -> dict:
    """Fetch the latest OHLCV candle for paper-trading TP/SL checks."""
    exchange_config = exchange_config or {}
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False),
        sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
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
    if not safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False):
        return []
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=True,
        sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
        market_type=exchange_config.get("market_type") or settings.exchange.market_type,
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
    )
    try:
        positions = await asyncio.to_thread(exchange.fetch_positions)
        result = []
        for pos in positions:
            try:
                raw_contracts = float(pos.get('contracts') or 0)
            except (TypeError, ValueError):
                raw_contracts = 0.0
            contracts = abs(raw_contracts)
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
                        if entry > 0 and mark > 0:
                            side = _normalized_position_side(pos, raw_contracts)
                            if side == 'long':
                                percentage = ((mark - entry) / entry) * 100
                            elif side == 'short':
                                percentage = ((entry - mark) / entry) * 100
                    except (TypeError, ValueError, ZeroDivisionError):
                        pass

                # Fallback: calculate from unrealized_pnl / notional if available
                if percentage is None and unrealized_pnl is not None and notional:
                    try:
                        abs_notional = abs(float(notional))
                        if abs_notional > 0:
                            percentage = (float(unrealized_pnl) / abs_notional) * 100
                    except (TypeError, ValueError, ZeroDivisionError):
                        percentage = None
                result.append({
                    "symbol": pos.get('symbol'),
                    "side": _normalized_position_side(pos, raw_contracts),
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
                    "margin_mode": pos.get('marginMode'),
                    "_data_reliable": True,
                })
        return result
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch positions: {e}")
        if exchange_config.get("raise_on_error"):
            raise
        return []


async def fetch_single_position(ticker: str, exchange_config: dict | None = None) -> dict | None:
    """Fetch a single position for a specific ticker from the exchange.

    Uses fetch_positions with a symbol filter for more targeted verification.
    Returns the position dict if found, None if not found or on error.
    """
    exchange_config = exchange_config or {}
    if not safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False):
        return None
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=True,
        sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
        market_type=exchange_config.get("market_type") or settings.exchange.market_type,
        margin_mode=exchange_config.get("margin_mode") or settings.risk.margin_mode,
    )
    try:
        resolved = await asyncio.to_thread(_resolve_symbol, exchange, ticker, exchange_config.get("market_type", ""))
        positions = await asyncio.to_thread(exchange.fetch_positions, [resolved])
        from core.utils.common import position_symbol_key as _psk
        ticker_key = _psk(ticker)
        for pos in positions:
            try:
                raw_contracts = float(pos.get('contracts') or 0)
            except (TypeError, ValueError):
                raw_contracts = 0.0
            contracts = abs(raw_contracts)
            if contracts != 0 and _psk(pos.get('symbol', '')) == ticker_key:
                return {
                    "symbol": pos.get('symbol'),
                    "side": _normalized_position_side(pos, raw_contracts),
                    "contracts": contracts,
                    "entryPrice": pos.get('entryPrice'),
                    "entry_price": pos.get('entryPrice'),
                    "markPrice": pos.get('markPrice'),
                    "mark_price": pos.get('markPrice'),
                    "notional": pos.get('notional'),
                    "unrealizedPnl": pos.get('unrealizedPnl'),
                    "unrealized_pnl": pos.get('unrealizedPnl'),
                    "liquidationPrice": pos.get('liquidationPrice'),
                    "liquidation_price": pos.get('liquidationPrice'),
                    "percentage": pos.get('percentage'),
                    "leverage": pos.get('leverage'),
                    "marginMode": pos.get('marginMode'),
                    "margin_mode": pos.get('marginMode'),
                }
        return None
    except Exception as e:
        logger.warning(f"[Exchange] fetch_single_position failed for {ticker}: {e}")
        raise


def _open_order_field(order: dict[str, Any], info: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = order.get(key)
        if value not in (None, ""):
            return value
        value = info.get(key)
        if value not in (None, ""):
            return value
    return None


def _normalize_open_order(order: dict[str, Any], source: str = "open_order") -> dict[str, Any]:
    info = order.get("info") if isinstance(order.get("info"), dict) else {}
    order_id = _open_order_field(order, info, "id", "algoId", "ordId", "clOrdId", "algoClOrdId")
    amount = _open_order_field(order, info, "amount", "sz")
    remaining = _open_order_field(order, info, "remaining", "amount", "sz")
    status = _open_order_field(order, info, "status", "state") or "open"
    return {
        "id": order_id,
        "symbol": _open_order_field(order, info, "symbol", "instId"),
        "side": _open_order_field(order, info, "side"),
        "type": _open_order_field(order, info, "type", "ordType"),
        "price": _open_order_field(order, info, "price", "px", "triggerPx", "tpTriggerPx", "slTriggerPx"),
        "amount": amount,
        "filled": _open_order_field(order, info, "filled", "accFillSz") or 0,
        "remaining": remaining or 0,
        "status": status,
        "timestamp": _open_order_field(order, info, "timestamp", "cTime"),
        "datetime": _open_order_field(order, info, "datetime"),
        "source": source,
        "info": info or order,
    }


def _dedupe_open_orders(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for order in orders:
        order_id = str(order.get("id") or "")
        key = order_id or f"{order.get('source')}:{order.get('symbol')}:{order.get('side')}:{order.get('type')}:{order.get('price')}"
        if key in seen:
            continue
        seen.add(key)
        deduped.append(order)
    return deduped


def _okx_inst_id(exchange: ccxt.Exchange, resolved_symbol: str | None) -> str | None:
    if not resolved_symbol:
        return None
    try:
        market = exchange.market(resolved_symbol)
    except Exception:
        market = (getattr(exchange, "markets", None) or {}).get(resolved_symbol)
    if isinstance(market, dict):
        info = market.get("info") if isinstance(market.get("info"), dict) else {}
        inst_id = market.get("id") or info.get("instId")
        if inst_id:
            return str(inst_id)

    if ":" in resolved_symbol:
        base_quote = resolved_symbol.split(":", 1)[0]
        return f"{base_quote.replace('/', '-')}-SWAP"
    return resolved_symbol.replace("/", "-")


async def _call_okx_pending_algo_orders(exchange: ccxt.Exchange, params: dict[str, Any]) -> Any:
    method = getattr(exchange, "privateGetTradeOrdersAlgoPending", None) or getattr(
        exchange, "private_get_trade_orders_algo_pending", None
    )
    if not method:
        return {"data": []}
    result = await asyncio.to_thread(method, params)
    if inspect.isawaitable(result):
        result = await result
    return result


def _extract_okx_algo_orders(response: Any) -> list[dict[str, Any]]:
    if isinstance(response, dict):
        data = response.get("data") or []
    else:
        data = response or []
    return [item for item in data if isinstance(item, dict)]


async def _fetch_okx_open_algo_orders(exchange: ccxt.Exchange, resolved_symbol: str | None = None) -> list[dict[str, Any]]:
    base_params: dict[str, Any] = {}
    inst_id = _okx_inst_id(exchange, resolved_symbol)
    if inst_id:
        base_params["instId"] = inst_id

    try:
        response = await _call_okx_pending_algo_orders(exchange, base_params)
        return [_normalize_open_order(order, source="okx_algo") for order in _extract_okx_algo_orders(response)]
    except Exception as first_exc:
        fallback_orders: list[dict[str, Any]] = []
        for ord_type in ("conditional", "oco", "trigger", "move_order_stop", "trailing_stop"):
            try:
                response = await _call_okx_pending_algo_orders(exchange, {**base_params, "ordType": ord_type})
                fallback_orders.extend(_extract_okx_algo_orders(response))
            except Exception as exc:
                logger.debug(f"[Exchange] OKX algo order query failed for ordType={ord_type}: {exc}")
        if fallback_orders:
            return [_normalize_open_order(order, source="okx_algo") for order in fallback_orders]
        # P0-FIX: Re-raise when all queries fail. Empty list would cause _verify_protective_orders
        # to think all TP/SL orders are missing and re-create them, leading to duplicates.
        # By raising, we let the caller's exception handler skip verification safely.
        logger.warning(
            f"[Exchange] OKX algo order query failed and fallback returned no orders. "
            f"Re-raising to prevent false 'missing order' detection. "
            f"Error: {first_exc}"
        )
        raise first_exc


async def get_open_orders(symbol: str | None = None, exchange_config: dict | None = None) -> list[dict]:
    """Fetch open/pending orders from exchange."""
    exchange_config = exchange_config or {}
    if not safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False):
        return []
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=True,
        sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
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
            resolved_symbol = None
            orders = await asyncio.to_thread(exchange.fetch_open_orders)

        normalized_orders = [_normalize_open_order(o) for o in orders if isinstance(o, dict)]
        if _exchange_id(exchange) == "okx":
            try:
                normalized_orders.extend(await _fetch_okx_open_algo_orders(exchange, resolved_symbol))
            except Exception as exc:
                logger.warning(f"[Exchange] Failed to fetch OKX pending algo orders: {exc}")
                if exchange_config.get("require_algo_orders") or exchange_config.get("raise_on_error"):
                    raise

        return _dedupe_open_orders(normalized_orders)
    except Exception as e:
        logger.error(f"[Exchange] Failed to fetch open orders: {e}")
        if exchange_config.get("raise_on_error") or exchange_config.get("require_algo_orders"):
            raise
        return []


async def get_recent_orders(symbol: str | None = None, limit: int = 50, exchange_config: dict | None = None) -> list[dict]:
    """Fetch recent closed orders from exchange."""
    exchange_config = exchange_config or {}
    if not safe_bool(exchange_config.get("live_trading", settings.exchange.live_trading), False):
        return []
    exchange = _get_or_create_exchange(
        exchange_id=exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name,
        api_key=_credential_from_exchange_config(exchange_config, "api_key", settings.exchange.api_key),
        api_secret=_credential_from_exchange_config(exchange_config, "api_secret", settings.exchange.api_secret),
        password=_credential_from_exchange_config(exchange_config, "password", settings.exchange.password),
        live=True,
        sandbox=safe_bool(exchange_config.get("sandbox_mode", settings.exchange.sandbox_mode), False),
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
