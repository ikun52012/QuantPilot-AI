"""
Signal Server - Signal Processing Service
Handles the complete signal processing pipeline.
"""
import asyncio
import hashlib
import json
import os
import time as _time
from collections.abc import Sequence
from datetime import datetime, timezone

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ai_analyzer import analyze_signal
from core.config import settings
from core.database import (
    PositionModel,
    close_position_async,
    get_user_active_subscription,
    get_user_by_id,
    has_recent_webhook_event,
    log_trade_db,
    record_webhook_event,
)
from core.metrics import (
    record_ai_analysis,
    record_prefilter_result,
    record_signal_received,
    record_trade,
)
from core.security import decrypt_settings_payload
from core.trading_control import trading_allowed
from core.utils.common import (
    first_valid,
    loads_list,
    position_symbol_key,
    resolve_limit_timeout_secs,
    safe_float,
    safe_int,
)
from exchange import cancel_order, execute_trade
from market_data import fetch_enhanced_market_context, fetch_market_context
from models import (
    AIAnalysis,
    MarketContext,
    PreFilterResult,
    SignalDirection,
    TradeDecision,
    TradingViewSignal,
)
from notifier import (
    notify_ai_analysis,
    notify_error,
    notify_pre_filter_blocked,
    notify_signal_received,
    notify_trade_executed,
)
from pre_filter import run_pre_filter_async
from services.order_reconciler import record_order_event

_WEBHOOK_LOCKS: dict[str, asyncio.Lock] = {}
_WEBHOOK_LOCKS_GUARD = asyncio.Lock()
_SENSITIVE_EVENT_KEY_PARTS = ("secret", "token", "password", "api_key", "api_secret")

# Per-ticker locks for concurrent signal handling
_TICKER_LOCKS: dict[str, asyncio.Lock] = {}
_TICKER_LOCKS_GUARD = asyncio.Lock()
_TICKER_LOCK_MAX_SIZE = 1000

# Per-ticker pending signal count for queue backpressure
_TICKER_PENDING: dict[str, int] = {}
_TICKER_PENDING_GUARD = asyncio.Lock()

# Global processing semaphore and interval control
_GLOBAL_PROCESSING_SEMAPHORE: asyncio.Semaphore | None = None
_GLOBAL_PROCESSING_GUARD = asyncio.Lock()
_LAST_SIGNAL_PROCESS_TIME: float = 0.0
_PROCESSING_INTERVAL_SEMAPHORE = asyncio.Lock()

# Dynamic interval tracking (Optimization 1)
_AI_RESPONSE_TIMES: list[float] = []
_AI_RESPONSE_TIMES_GUARD = asyncio.Lock()
_AI_RESPONSE_TIMES_MAX_SAMPLES = 20

# Batch processing state (Optimization 4)
_PENDING_BATCH_SIGNALS: dict[str, list[tuple[TradingViewSignal, float, dict | None]]] = {}
_BATCH_SIGNALS_GUARD = asyncio.Lock()

# Prefetch market data cache (Optimization 5)
_PREFETCHED_MARKET_DATA: dict[str, tuple[float, MarketContext]] = {}
_PREFETCH_GUARD = asyncio.Lock()


async def _track_ai_response_time(response_time: float) -> None:
    """Track AI response time for dynamic interval adjustment."""
    async with _AI_RESPONSE_TIMES_GUARD:
        _AI_RESPONSE_TIMES.append(response_time)
        if len(_AI_RESPONSE_TIMES) > _AI_RESPONSE_TIMES_MAX_SAMPLES:
            _AI_RESPONSE_TIMES.pop(0)


async def _get_avg_ai_response_time() -> float:
    """Get average AI response time from recent samples."""
    async with _AI_RESPONSE_TIMES_GUARD:
        if not _AI_RESPONSE_TIMES:
            return 0.0
        return sum(_AI_RESPONSE_TIMES) / len(_AI_RESPONSE_TIMES)


async def _get_dynamic_interval() -> float:
    """Calculate dynamic interval based on AI load (Optimization 1).

    High load (>30s avg response) -> double interval
    Normal load -> use base interval
    """
    if not settings.ai.dynamic_interval_enabled:
        return settings.ai.signal_processing_interval_secs

    avg_time = await _get_avg_ai_response_time()
    base_interval = settings.ai.signal_processing_interval_secs

    if avg_time > settings.ai.dynamic_interval_high_load_threshold:
        dynamic_interval = base_interval * settings.ai.dynamic_interval_high_load_multiplier
        logger.info(
            f"[SignalProcessor] High AI load detected (avg={avg_time:.1f}s), "
            f"increasing interval: {base_interval:.1f}s -> {dynamic_interval:.1f}s"
        )
        return dynamic_interval
    return base_interval


async def _get_global_semaphore() -> asyncio.Semaphore:
    """Get or create global processing semaphore (lazy init)."""
    global _GLOBAL_PROCESSING_SEMAPHORE
    async with _GLOBAL_PROCESSING_GUARD:
        if _GLOBAL_PROCESSING_SEMAPHORE is None:
            _GLOBAL_PROCESSING_SEMAPHORE = asyncio.Semaphore(settings.ai.global_processing_semaphore)
        return _GLOBAL_PROCESSING_SEMAPHORE


async def _wait_processing_interval(skip_interval: bool = False) -> None:
    """Wait for processing interval after completing a signal (Optimization 1 & 2).

    Args:
        skip_interval: If True, skip waiting (for high-confidence signals)
    """
    global _LAST_SIGNAL_PROCESS_TIME
    if skip_interval:
        logger.debug("[SignalProcessor] Skipping interval (high confidence signal)")
        return

    interval = await _get_dynamic_interval()
    if interval <= 0:
        return

    async with _PROCESSING_INTERVAL_SEMAPHORE:
        now = _time.time()
        elapsed = now - _LAST_SIGNAL_PROCESS_TIME
        if elapsed < interval:
            wait_time = interval - elapsed
            logger.debug(f"[SignalProcessor] Waiting {wait_time:.1f}s before next signal")
            await asyncio.sleep(wait_time)
        _LAST_SIGNAL_PROCESS_TIME = _time.time()


async def _prefetch_market_data_async(ticker: str) -> MarketContext | None:
    """Prefetch market data before acquiring semaphore (Optimization 5).

    This allows market data fetch to happen in parallel with other signals,
    reducing overall latency when semaphore is acquired.
    """
    if not settings.ai.prefetch_market_data:
        return None

    cache_key = ticker.upper().strip()
    now = _time.time()

    async with _PREFETCH_GUARD:
        cached = _PREFETCHED_MARKET_DATA.get(cache_key)
        if cached and (now - cached[0]) < 30:
            logger.debug(f"[SignalProcessor] Using prefetched market data for {ticker}")
            return cached[1]

    try:
        enhanced_filters = settings.ai.voting_enabled or os.getenv("ENHANCED_FILTERS_ENABLED", "true").lower() == "true"
        if enhanced_filters:
            market = await fetch_enhanced_market_context(ticker)
        else:
            market = await fetch_market_context(ticker)

        async with _PREFETCH_GUARD:
            _PREFETCHED_MARKET_DATA[cache_key] = (now, market)
            if len(_PREFETCHED_MARKET_DATA) > 100:
                oldest_key = next(iter(_PREFETCHED_MARKET_DATA))
                _PREFETCHED_MARKET_DATA.pop(oldest_key)

        return market
    except Exception as e:
        logger.warning(f"[SignalProcessor] Prefetch market data failed for {ticker}: {e}")
        return None


async def _check_batch_signals(ticker: str, signal: TradingViewSignal, raw_body: dict | None) -> bool:
    """Check if signal should be batched with similar pending signals (Optimization 4).

    Returns True if signal was batched (should not process individually).
    """
    if not settings.ai.batch_signals_enabled:
        return False

    key = ticker.upper().strip()
    now = _time.time()

    async with _BATCH_SIGNALS_GUARD:
        pending = _PENDING_BATCH_SIGNALS.get(key, [])

        expired = [(s, t, b) for s, t, b in pending if now - t > settings.ai.batch_signals_window_secs]
        for s, t, b in expired:
            pending.remove((s, t, b))

        same_direction = [
            (s, t, b) for s, t, b in pending
            if s.direction == signal.direction
        ]

        if len(same_direction) >= settings.ai.batch_signals_max_count:
            logger.info(
                f"[SignalProcessor] Batch triggered for {ticker} {signal.direction.value}: "
                f"{len(same_direction) + 1} signals within {settings.ai.batch_signals_window_secs}s window"
            )
            for s, t, b in same_direction:
                pending.remove((s, t, b))
            _PENDING_BATCH_SIGNALS[key] = pending
            return False

        pending.append((signal, now, raw_body))
        _PENDING_BATCH_SIGNALS[key] = pending

        if len(same_direction) >= 1:
            logger.debug(
                f"[SignalProcessor] Signal batching pending for {ticker}: "
                f"{len(same_direction) + 1}/{settings.ai.batch_signals_max_count} same-direction signals"
            )

        return False


async def _ticker_lock(ticker: str, user_id: str | None = None) -> asyncio.Lock:
    """Get or create a lock for a specific ticker to prevent concurrent conflicts.

    This ensures that signals for the same ticker are processed sequentially,
    preventing race conditions when two opposite signals arrive simultaneously.

    Args:
        ticker: The ticker symbol (e.g., "BTCUSDT")
        user_id: Optional user ID for multi-user isolation

    Returns:
        asyncio.Lock for this ticker+user combination
    """
    scope = user_id or "admin"
    key = f"{scope}:{ticker.upper().strip()}"

    async with _TICKER_LOCKS_GUARD:
        lock = _TICKER_LOCKS.get(key)
        if lock is None:
            lock = asyncio.Lock()
            _TICKER_LOCKS[key] = lock

            if len(_TICKER_LOCKS) > _TICKER_LOCK_MAX_SIZE:
                keys_to_remove = list(_TICKER_LOCKS.keys())[:_TICKER_LOCK_MAX_SIZE // 2]
                for k in keys_to_remove:
                    if k == key:
                        continue
                    old_lock = _TICKER_LOCKS.get(k)
                    if old_lock and not old_lock.locked():
                        _TICKER_LOCKS.pop(k, None)

        return lock


async def _check_ticker_queue_limit(ticker: str, user_id: str | None = None) -> bool:
    """Check if ticker queue has room for another signal.

    Returns:
        True if queue has room, False if queue is full
    """
    scope = user_id or "admin"
    key = f"{scope}:{ticker.upper().strip()}"
    limit = settings.ai.signal_queue_limit

    async with _TICKER_PENDING_GUARD:
        current = _TICKER_PENDING.get(key, 0)
        if current >= limit:
            logger.warning(
                f"[SignalProcessor] Ticker {ticker} queue full: "
                f"{current}/{limit} pending signals, rejecting new signal"
            )
            return False
        _TICKER_PENDING[key] = current + 1
        return True


async def _release_ticker_queue_slot(ticker: str, user_id: str | None = None) -> None:
    """Release a ticker queue slot after signal processing completes."""
    scope = user_id or "admin"
    key = f"{scope}:{ticker.upper().strip()}"

    async with _TICKER_PENDING_GUARD:
        current = _TICKER_PENDING.get(key, 0)
        _TICKER_PENDING[key] = max(0, current - 1)


async def _release_ticker_lock(ticker: str, user_id: str | None = None) -> None:
    """Release a ticker lock after processing.

    Note: Locks are automatically released when the async context exits,
    but this function can be called to explicitly cleanup unused locks.
    """
    scope = user_id or "admin"
    key = f"{scope}:{ticker.upper().strip()}"

    async with _TICKER_LOCKS_GUARD:
        lock = _TICKER_LOCKS.get(key)
        if lock and not lock.locked():
            _TICKER_LOCKS.pop(key, None)


async def _fingerprint_lock(fingerprint: str) -> asyncio.Lock:
    async with _WEBHOOK_LOCKS_GUARD:
        lock = _WEBHOOK_LOCKS.get(fingerprint)
        if lock is None:
            lock = asyncio.Lock()
            _WEBHOOK_LOCKS[fingerprint] = lock
        return lock


async def _release_fingerprint_lock(fingerprint: str, lock: asyncio.Lock) -> None:
    async with _WEBHOOK_LOCKS_GUARD:
        if not lock.locked() and _WEBHOOK_LOCKS.get(fingerprint) is lock:
            _WEBHOOK_LOCKS.pop(fingerprint, None)


def _safe_event_payload(value):
    """Redact secrets before webhook payloads are stored in event logs."""
    if isinstance(value, dict):
        safe = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in _SENSITIVE_EVENT_KEY_PARTS):
                safe[key] = "***"
            else:
                safe[key] = _safe_event_payload(item)
        return safe
    if isinstance(value, list):
        return [_safe_event_payload(item) for item in value]
    return value


# ─────────────────────────────────────────────
# Webhook Fingerprint
# ─────────────────────────────────────────────

def compute_webhook_fingerprint(body: dict, user_id: str | None = None) -> str:
    """Compute a unique fingerprint for webhook deduplication."""
    scope = user_id or "admin"
    alert_id = str(body.get("alert_id") or body.get("order_id") or body.get("id") or "").strip()

    fields = {
        "scope": scope,
        "secret_hash": hashlib.sha256(str(body.get("secret", "")).strip().encode()).hexdigest()[:16],
        "ticker": str(body.get("ticker", "")).upper().strip(),
        "direction": str(body.get("direction", "")).lower().strip(),
        "timeframe": str(body.get("timeframe", "")).strip(),
        "price": round(float(body.get("price") or 0), 8),
        "strategy": str(body.get("strategy", "")).strip(),
        "message": str(body.get("message", "")).strip(),
    }

    if alert_id:
        fields = {"scope": scope, "secret_hash": fields["secret_hash"], "alert_id": alert_id}

    raw = json.dumps(fields, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


# ─────────────────────────────────────────────
# Signal Processing Pipeline
# ─────────────────────────────────────────────

class SignalProcessor:
    """Main signal processing service."""

    def __init__(self, session: AsyncSession):
        self.session = session

    async def process_webhook(
        self,
        signal: TradingViewSignal,
        user_id: str | None = None,
        client_ip: str = "",
        raw_body: dict | None = None,
    ) -> dict:
        """
        Process a webhook signal through the complete pipeline.
        Returns the result of the processing.

        Architecture (6 Optimizations):
        1. Dynamic interval based on AI load
        2. Priority skip interval for high-confidence signals
        3. Prefetch market data before semaphore
        4. Batch similar signals (same ticker+direction)
        5. Global semaphore (5 concurrent max)
        6. Per-ticker lock prevents conflicts

        Flow:
        1. Prefetch market data (parallel optimization)
        2. Check batch signals
        3. Check queue limit
        4. Acquire global semaphore (waits if 5 processing)
        5. Acquire ticker lock (waits if same ticker)
        6. Process signal with prefetched market data
        7. Check confidence for interval skip
        8. Wait dynamic interval
        9. Release semaphore and lock
        """
        # Optimization 5: Prefetch market data BEFORE acquiring semaphore
        prefetched_market = await _prefetch_market_data_async(signal.ticker)

        # Optimization 4: Check batch signals
        batched = await _check_batch_signals(signal.ticker, signal, raw_body)
        if batched:
            return {"status": "batched", "reason": "Signal added to batch queue"}

        # Check queue limit first (fast rejection for extreme load)
        if not await _check_ticker_queue_limit(signal.ticker, user_id):
            return {
                "status": "rejected",
                "reason": f"Queue full for {signal.ticker} - too many pending signals",
                "queue_limit": settings.ai.signal_queue_limit,
            }

        # Get global processing semaphore
        global_sem = await _get_global_semaphore()

        # Wait for global semaphore (max 5 concurrent signals)
        async with global_sem:
            # Acquire ticker-specific lock to prevent same-ticker conflicts
            ticker_lock = await _ticker_lock(signal.ticker, user_id)

            try:
                async with ticker_lock:
                    # Process signal with prefetched market data
                    result = await self._process_signal_locked(
                        signal, user_id, client_ip, raw_body, prefetched_market=prefetched_market
                    )

                # Optimization 2: Check confidence for interval skip
                skip_interval = False
                if result.get("status") in ("filled", "simulated"):
                    analysis_data = result.get("analysis", {})
                    analysis_confidence = analysis_data.get("confidence", 0.0) if isinstance(analysis_data, dict) else 0.0
                    if analysis_confidence >= settings.ai.priority_skip_interval_confidence_threshold:
                        skip_interval = True
                        logger.info(
                            f"[SignalProcessor] High confidence signal ({analysis_confidence:.2f}) "
                            f"skips interval for faster next signal"
                        )

                # Optimization 1: Wait dynamic interval
                await _wait_processing_interval(skip_interval=skip_interval)

                return result
            finally:
                # Always release queue slot when done
                await _release_ticker_queue_slot(signal.ticker, user_id)

    async def _process_signal_locked(
        self,
        signal: TradingViewSignal,
        user_id: str | None = None,
        client_ip: str = "",
        raw_body: dict | None = None,
        prefetched_market: MarketContext | None = None,
    ) -> dict:
        """Internal signal processing with ticker lock already acquired.

        Args:
            prefetched_market: Pre-fetched market data (Optimization 5)
        """
        # Compute fingerprint for deduplication
        fingerprint = compute_webhook_fingerprint(raw_body or signal.model_dump(), user_id)
        user_settings = await self._load_user_settings(user_id)

        # Reserve the webhook before slow AI/exchange calls so concurrent or
        # retried TradingView deliveries cannot pass the dedupe check together.
        reservation = await self._reserve_webhook_event(
            fingerprint=fingerprint,
            signal=signal,
            user_id=user_id,
            client_ip=client_ip,
            payload=raw_body or signal.model_dump(),
        )
        if reservation is None:
            logger.warning(f"[Signal] Duplicate webhook: {fingerprint[:16]}")
            return {"status": "duplicate", "reason": "Duplicate signal within 5 minutes"}

        # Record signal received
        record_signal_received(signal.ticker, signal.direction.value, user_id)

        # Notify signal received
        await notify_signal_received(signal.ticker, signal.direction.value, signal.price)

        try:
            # Step 1: Fetch market context (use prefetched if available)
            enhanced_filters = settings.ai.voting_enabled or os.getenv("ENHANCED_FILTERS_ENABLED", "true").lower() == "true"
            if prefetched_market:
                market = prefetched_market
                logger.debug(f"[SignalProcessor] Using prefetched market data for {signal.ticker}")
            elif enhanced_filters:
                market = await fetch_enhanced_market_context(signal.ticker)
            else:
                market = await fetch_market_context(signal.ticker)

            # Step 2: Run pre-filter
            prefilter_result = await self._run_prefilter(signal, market, user_id, user_settings)

            if not prefilter_result.passed:
                await self._record_and_notify_blocked(
                    reservation, signal, fingerprint, user_id, client_ip, prefilter_result.reason, raw_body
                )
                return {
                    "status": "blocked",
                    "reason": prefilter_result.reason,
                    "checks": prefilter_result.checks,
                }

            # Step 3: AI Analysis
            analysis = await self._run_ai_analysis(signal, market, user_settings, prefilter_result)

            # Step 4: Build trade decision
            decision = self._build_trade_decision(signal, analysis, market, user_id, user_settings)

            # Step 5: Check for conflicting open positions
            if decision.execute:
                conflict_reason, conflicting_position = await self._check_position_conflict(
                    decision, user_id, user_settings
                )
                if conflict_reason and conflicting_position:
                    # Close existing position before opening reverse position
                    close_result = await self._close_conflicting_position(
                        conflicting_position, user_id, user_settings
                    )
                    if close_result.get("status") == "error":
                        decision.execute = False
                        decision.reason = f"Failed to close existing position: {close_result.get('reason')}"
                    else:
                        logger.info(
                            f"[Signal] Reverse signal: closed existing {conflicting_position.direction} position "
                            f"on {decision.ticker}, proceeding with {decision.direction.value} trade"
                        )
                    # Refresh session to clear closed position state
                    await self.session.flush()

            # Step 6: Check correlation risk (same-direction concentration)
            if decision.execute:
                correlation_risk = await self._check_correlation_risk(decision, user_id, user_settings)
                if correlation_risk.get("exceeded"):
                    decision.execute = False
                    decision.reason = correlation_risk.get("reason")

            # Step 7: Execute trade
            if decision.execute:
                result = await self._execute_trade(decision, user_id, user_settings)
            else:
                result = {"status": "rejected", "reason": decision.reason}

            self._update_reserved_event(
                reservation,
                status=result.get("status", "processed"),
                status_code=200,
                reason=result.get("reason", ""),
                payload=raw_body or signal.model_dump(),
            )

            # Add analysis to result for skip_interval check
            if analysis:
                result["analysis"] = analysis.model_dump()

            return result

        except Exception as e:
            logger.error(f"[Signal] Processing error: {e}")
            await notify_error(str(e))

            self._update_reserved_event(
                reservation,
                status="error",
                status_code=500,
                reason=str(e),
                payload=raw_body or signal.model_dump(),
            )

            return {"status": "error", "reason": str(e)}

    async def _reserve_webhook_event(
        self,
        fingerprint: str,
        signal: TradingViewSignal,
        user_id: str | None,
        client_ip: str,
        payload: dict,
    ):
        """Reserve a webhook fingerprint before slow processing starts."""
        lock = await _fingerprint_lock(fingerprint)
        try:
            async with lock:
                if await has_recent_webhook_event(self.session, fingerprint, window_secs=300):
                    return None
                event = await record_webhook_event(
                    session=self.session,
                    user_id=user_id,
                    fingerprint=fingerprint,
                    ticker=signal.ticker,
                    direction=signal.direction.value,
                    status="received",
                    status_code=202,
                    reason="reserved",
                    client_ip=client_ip,
                    payload=_safe_event_payload(payload),
                )
                await self.session.commit()
                return event
        finally:
            await _release_fingerprint_lock(fingerprint, lock)

    @staticmethod
    def _update_reserved_event(event, status: str, status_code: int, reason: str, payload: dict) -> None:
        event.status = status
        event.status_code = status_code
        event.reason = reason or ""
        event.payload_json = json.dumps(_safe_event_payload(payload or {}), default=str)

    async def _run_prefilter(
        self,
        signal: TradingViewSignal,
        market: MarketContext,
        user_id: str | None,
        user_settings: dict | None = None,
    ) -> PreFilterResult:
        """Run pre-filter checks."""
        from pre_filter import get_thresholds

        # Get user settings for limits
        max_daily_trades = int(getattr(settings.risk, "max_daily_trades", 0) or 0)
        max_daily_loss = float(getattr(settings.risk, "max_daily_loss_pct", 0.0) or 0.0)
        thresholds = get_thresholds()
        min_pass_score = float(thresholds.get("min_pass_score", signal.ticker) or 0.0)
        use_scoring = min_pass_score > 0.0

        user_risk = (user_settings or {}).get("risk") or {}
        if user_risk:
            # BUG FIX: Validate user risk settings to prevent bypass of risk controls.
            # Negative or extreme values could disable safety limits entirely.
            raw_daily_trades = user_risk.get("max_daily_trades")
            if raw_daily_trades is not None:
                try:
                    max_daily_trades = max(1, min(int(float(raw_daily_trades)), 200))
                except (TypeError, ValueError):
                    pass

            raw_daily_loss = user_risk.get("max_daily_loss_pct")
            if raw_daily_loss is not None:
                try:
                    max_daily_loss = max(0.1, min(float(raw_daily_loss), 100.0))
                except (TypeError, ValueError):
                    pass

        result = await run_pre_filter_async(
            signal=signal,
            market=market,
            max_daily_trades=max_daily_trades,
            max_daily_loss_pct=max_daily_loss,
            user_id=user_id,
            use_scoring=use_scoring,
            min_pass_score=min_pass_score,
        )

        record_prefilter_result(
            signal.ticker,
            signal.direction.value,
            result.passed,
            result.reason,
        )

        return result

    async def _run_ai_analysis(
        self,
        signal: TradingViewSignal,
        market: MarketContext,
        user_settings: dict | None = None,
prefilter_result: PreFilterResult | None = None,
    ) -> AIAnalysis:
        """Run AI analysis on the signal."""
        import time
        start = time.time()

        scoped_user_settings = dict(user_settings or {})
        if prefilter_result is not None:
            soft_fail_count = sum(1 for check in prefilter_result.checks.values() if check.get("soft_fail", False))
            hard_fail_count = sum(
                1
                for check in prefilter_result.checks.values()
                if not check.get("passed", True) and not check.get("disabled", False) and not check.get("soft_fail", False)
            )
            missing_data_count = sum(1 for check in prefilter_result.checks.values() if check.get("missing_data", False))
            notable_checks = []
            for check_name, check in prefilter_result.checks.items():
                if check.get("soft_fail", False) or not check.get("passed", True) or check.get("missing_data", False):
                    notable_checks.append(check_name)
            scoped_user_settings["_prefilter_summary"] = {
                "score": round(float(prefilter_result.score), 2),
                "soft_fail_count": soft_fail_count,
                "hard_fail_count": hard_fail_count,
                "missing_data_count": missing_data_count,
                "notable_checks": notable_checks[:6],
            }

        analysis = await analyze_signal(signal, market, scoped_user_settings)

        latency = time.time() - start
        # Optimization 1: Track AI response time for dynamic interval
        await _track_ai_response_time(latency)

        record_ai_analysis(
            settings.ai.provider,
            analysis.recommendation,
            analysis.confidence,
            latency,
        )

        await notify_ai_analysis(signal.ticker, analysis)

        return analysis

    def _build_trade_decision(
        self,
        signal: TradingViewSignal,
        analysis: AIAnalysis,
        market: MarketContext,
        user_id: str | None,
        user_settings: dict | None = None,
    ) -> TradeDecision:
        """Build trade decision from signal and analysis."""
        decision = TradeDecision(
            signal=signal,
            ai_analysis=analysis,
            ticker=signal.ticker,
            direction=signal.direction,
            entry_price=signal.price,
        )
        exchange_cfg = (user_settings or {}).get("exchange") or {}
        decision.order_type = str(
            exchange_cfg.get("default_order_type")
            or settings.exchange.default_order_type
            or "market"
        ).lower().strip()
        limit_timeout_overrides = (
            exchange_cfg.get("limit_timeout_overrides")
            if "limit_timeout_overrides" in exchange_cfg
            else settings.exchange.limit_timeout_overrides
        )
        decision.limit_timeout_secs = resolve_limit_timeout_secs(
            signal.timeframe,
            limit_timeout_overrides,
        )

        # Check AI recommendation
        if analysis.recommendation == "reject":
            decision.execute = False
            decision.reason = f"AI rejected: {analysis.reasoning}"
            return decision

        if analysis.confidence < 0.4:
            decision.execute = False
            decision.reason = f"Low confidence: {analysis.confidence:.2f}"
            return decision

        if (
            analysis.suggested_direction
            and analysis.suggested_direction != signal.direction
            and signal.direction in {SignalDirection.LONG, SignalDirection.SHORT}
        ):
            decision.execute = False
            decision.reason = (
                f"AI suggested {analysis.suggested_direction.value} but signal was "
                f"{signal.direction.value}; rejecting direction conflict"
            )
            return decision

        # Set execute flag
        decision.execute = analysis.recommendation in ("execute", "modify")

        # ── SMC/FVG entry optimization ──
        # When AI recommends "modify" and provides a suggested_entry, use it
        # as the optimal entry price instead of the raw signal price.
        # BUG FIX: If modify fails validation, fallback to original price instead of rejecting
        if decision.execute and analysis.recommendation == "modify":
            suggested = float(analysis.suggested_entry or 0)

            if suggested > 0:
                price_diff_pct = abs(suggested - signal.price) / signal.price * 100 if signal.price > 0 else 0

                # Only accept modified entry if it's within 5% of signal price
                if price_diff_pct <= 5.0:
                    logger.info(
                        f"[Signal] AI modified entry: {signal.price} → {suggested} "
                        f"({price_diff_pct:+.2f}% adjustment via SMC/FVG)"
                    )
                    decision.entry_price = suggested
                else:
                    # Fallback: suggested entry too far, use original price
                    logger.warning(
                        f"[Signal] AI suggested entry {suggested} is {price_diff_pct:.2f}% away from signal price, "
                        f"falling back to original signal price {signal.price}"
                    )
                    decision.entry_price = signal.price
                    # Don't reject the trade, just use original price
            else:
                # Fallback: no valid suggested_entry, use original signal price
                logger.warning(
                    f"[Signal] AI recommended modify without valid suggested_entry, "
                    f"using original signal price {signal.price}"
                )
                decision.entry_price = signal.price
                # Don't reject the trade, continue with original price

        if decision.execute:
            self._apply_exit_plan(decision, signal, analysis, market, user_settings or {})
            if signal.direction in {SignalDirection.LONG, SignalDirection.SHORT}:
                if not decision.stop_loss:
                    decision.execute = False
                    decision.reason = "No valid stop loss available for opening trade"
                    return decision
                if not decision.take_profit_levels:
                    decision.execute = False
                    decision.reason = "No valid take-profit target available for opening trade"
                    return decision
                # Validate R:R ratio
                rr_valid, rr_reason = self._validate_risk_reward_ratio(
                    decision.entry_price, decision.stop_loss,
                    decision.take_profit_levels, signal.direction, user_settings or {}
                )
                if not rr_valid:
                    decision.execute = False
                    decision.reason = rr_reason
                    return decision

        # Set trailing stop - use smart selector if not explicitly configured
        trailing_cfg = (user_settings or {}).get("trailing_stop") or {}
        user_trailing_mode = str(trailing_cfg.get("mode") or "").lower()

        # Determine trailing stop mode
        if user_trailing_mode and user_trailing_mode != "none" and user_trailing_mode != "auto":
            # User explicitly configured a mode (not "auto")
            trailing_mode = user_trailing_mode
            trailing_reason = "User configured trailing stop mode"
        elif user_trailing_mode == "auto" or not user_trailing_mode:
            # Use smart trailing stop selector
            from smart_trailing_stop import select_smart_trailing_stop
            from timeframe_exits import get_timeframe_config

            tf_config = get_timeframe_config(str(signal.timeframe or "60"))
            num_tp_levels = self._max_tp_levels(user_settings)

            trailing_decision = select_smart_trailing_stop(
                confidence=analysis.confidence,
                market_condition=analysis.market_condition,
                trend_strength=analysis.trend_strength or "moderate",
                risk_score=analysis.risk_score,
                timeframe=str(signal.timeframe or "60"),
                num_tp_levels=num_tp_levels,
                atr_pct=safe_float(market.atr_pct, tf_config.default_sl_pct),
                user_override=None,  # Auto mode, no override
            )
            trailing_mode = trailing_decision.mode.value
            trailing_reason = trailing_decision.reasoning

            # Log the smart selection
            logger.info(
                f"[Signal] Smart trailing stop selected: {trailing_mode} "
                f"(confidence={analysis.confidence:.2f}, market={analysis.market_condition}, "
                f"trend={analysis.trend_strength or 'moderate'}, reason={trailing_reason})"
            )
        else:
            # Default from settings
            trailing_mode = str(settings.trailing_stop.mode)
            trailing_reason = "Default from server settings"

        # Apply trailing stop if not "none"
        if trailing_mode != "none":
            from models import TrailingStopConfig, TrailingStopMode
            decision.trailing_stop = TrailingStopConfig(
                mode=TrailingStopMode(trailing_mode),
                trail_pct=safe_float(first_valid(trailing_cfg.get("trail_pct"), settings.trailing_stop.trail_pct), settings.trailing_stop.trail_pct),
                activation_profit_pct=safe_float(
                    first_valid(trailing_cfg.get("activation_profit_pct"), settings.trailing_stop.activation_profit_pct),
                    settings.trailing_stop.activation_profit_pct,
                ),
                trailing_step_pct=safe_float(
                    first_valid(trailing_cfg.get("trailing_step_pct"), settings.trailing_stop.trailing_step_pct),
                    settings.trailing_stop.trailing_step_pct,
                ),
            )

        # Calculate position size
        decision.quantity = self._calculate_position_size(
            market.current_price or signal.price,
            analysis.position_size_pct,
            analysis.recommended_leverage,
            decision=decision,
            user_settings=user_settings,
        )

        decision.reason = analysis.reasoning
        return decision

    def _apply_exit_plan(
        self,
        decision: TradeDecision,
        signal: TradingViewSignal,
        analysis: AIAnalysis,
        market: MarketContext,
        user_settings: dict,
    ) -> None:
        """Apply either custom configured exits or validated AI-generated exits.

        Enhanced validation:
        - Minimum SL/TP distance (based on ATR or percentage)
        - Maximum SL distance (prevent oversized risk)
        - R:R ratio validation
        """
        if signal.direction not in {SignalDirection.LONG, SignalDirection.SHORT}:
            return

        risk_cfg = user_settings.get("risk") or {}
        exit_mode = str(risk_cfg.get("exit_management_mode") or settings.risk.exit_management_mode)
        atr_pct = safe_float(market.atr_pct, 0.0)

        if exit_mode == "custom":
            self._apply_custom_exit_plan(decision, signal, user_settings, atr_pct)
            return

        # AI-generated exits with strict validation
        # BUG FIX: Use decision.entry_price (may be modified by AI) instead of signal.price
        entry_price = float(decision.entry_price or signal.price or 0)
        timeframe = str(signal.timeframe or "60")
        sl_price = self._valid_stop_loss(
            signal.direction, entry_price, analysis.suggested_stop_loss,
            atr_pct=atr_pct, user_settings=user_settings, timeframe=timeframe
        )
        decision.stop_loss = sl_price

        raw_levels = [
            (analysis.suggested_tp1, analysis.tp1_qty_pct),
            (analysis.suggested_tp2, analysis.tp2_qty_pct),
            (analysis.suggested_tp3, analysis.tp3_qty_pct),
            (analysis.suggested_tp4, analysis.tp4_qty_pct),
        ]
        max_levels = self._max_tp_levels(user_settings)
        decision.take_profit_levels = self._build_take_profit_levels(
            signal.direction, entry_price, raw_levels, max_levels,
            atr_pct=atr_pct, sl_price=sl_price, user_settings=user_settings, timeframe=timeframe
        )
        if decision.take_profit_levels:
            decision.take_profit = decision.take_profit_levels[0].price

    def _apply_custom_exit_plan(
        self,
        decision: TradeDecision,
        signal: TradingViewSignal,
        user_settings: dict,
        atr_pct: float = 0.0
    ) -> None:
        """Build fixed percentage SL/TP exits from admin configuration.

        Also validates against minimum/maximum distance requirements.
        """
        # BUG FIX: Use decision.entry_price (may be modified by AI) instead of signal.price
        entry = float(decision.entry_price or signal.price or 0)
        if entry <= 0:
            return

        timeframe = str(signal.timeframe or "60")
        risk_cfg = user_settings.get("risk") or {}
        tp_cfg = user_settings.get("take_profit") or {}
        stop_pct = max(0.01, safe_float(first_valid(risk_cfg.get("custom_stop_loss_pct"), settings.risk.custom_stop_loss_pct), 0.0))

        # Validate SL percentage against minimum requirements
        min_sl_pct = self._get_min_sl_percentage(atr_pct, user_settings, timeframe)
        max_sl_pct = self._get_max_sl_percentage(user_settings, timeframe)
        if stop_pct < min_sl_pct:
            logger.warning(f"[Signal] Custom SL {stop_pct}% below minimum {min_sl_pct}%, adjusting")
            stop_pct = min_sl_pct
        if stop_pct > max_sl_pct:
            logger.warning(f"[Signal] Custom SL {stop_pct}% above maximum {max_sl_pct}%, adjusting")
            stop_pct = max_sl_pct

        tp1_pct = safe_float(first_valid(tp_cfg.get("tp1_pct"), settings.take_profit.tp1_pct), settings.take_profit.tp1_pct)
        tp2_pct = safe_float(first_valid(tp_cfg.get("tp2_pct"), settings.take_profit.tp2_pct), settings.take_profit.tp2_pct)
        tp3_pct = safe_float(first_valid(tp_cfg.get("tp3_pct"), settings.take_profit.tp3_pct), settings.take_profit.tp3_pct)
        tp4_pct = safe_float(first_valid(tp_cfg.get("tp4_pct"), settings.take_profit.tp4_pct), settings.take_profit.tp4_pct)

        # Validate TP percentages against minimum
        min_tp_pct = self._get_min_tp_percentage(atr_pct, user_settings, timeframe)
        tp1_pct = max(min_tp_pct, tp1_pct)

        tp1_qty = safe_float(first_valid(tp_cfg.get("tp1_qty"), settings.take_profit.tp1_qty), settings.take_profit.tp1_qty)
        tp2_qty = safe_float(first_valid(tp_cfg.get("tp2_qty"), settings.take_profit.tp2_qty), settings.take_profit.tp2_qty)
        tp3_qty = safe_float(first_valid(tp_cfg.get("tp3_qty"), settings.take_profit.tp3_qty), settings.take_profit.tp3_qty)
        tp4_qty = safe_float(first_valid(tp_cfg.get("tp4_qty"), settings.take_profit.tp4_qty), settings.take_profit.tp4_qty)

        if signal.direction == SignalDirection.LONG:
            decision.stop_loss = round(entry * (1 - stop_pct / 100.0), 8)
            raw_levels = [
                (entry * (1 + tp1_pct / 100.0), tp1_qty),
                (entry * (1 + tp2_pct / 100.0), tp2_qty),
                (entry * (1 + tp3_pct / 100.0), tp3_qty),
                (entry * (1 + tp4_pct / 100.0), tp4_qty),
            ]
        else:
            decision.stop_loss = round(entry * (1 + stop_pct / 100.0), 8)
            raw_levels = [
                (entry * (1 - tp1_pct / 100.0), tp1_qty),
                (entry * (1 - tp2_pct / 100.0), tp2_qty),
                (entry * (1 - tp3_pct / 100.0), tp3_qty),
                (entry * (1 - tp4_pct / 100.0), tp4_qty),
            ]

        decision.take_profit_levels = self._build_take_profit_levels(
            signal.direction,
            entry,
            raw_levels,
            self._max_tp_levels(user_settings),
            atr_pct=atr_pct,
            sl_price=decision.stop_loss,
            user_settings=user_settings,
            timeframe=timeframe,
        )
        if decision.take_profit_levels:
            decision.take_profit = decision.take_profit_levels[0].price

    def _build_take_profit_levels(
        self,
        direction: SignalDirection,
        entry: float,
        raw_levels: Sequence[tuple[float | None, float]],
        max_levels: int,
        atr_pct: float = 0.0,
        sl_price: float | None = None,
        user_settings: dict | None = None,
        timeframe: str = "60",
    ) -> list:
        """Validate TP direction, distance, and cap cumulative close quantity to 100%.

        Enhanced validation:
        - Minimum TP distance (ATR-based or percentage floor)
        - Maximum TP distance (timeframe-based, warns if exceeded)
        - R:R ratio check (TP distance vs SL distance)
        """
        from models import TakeProfitLevel
        from timeframe_exits import get_max_tp_for_timeframe

        min_tp_pct = self._get_min_tp_percentage(atr_pct, user_settings or {}, timeframe)
        max_tp_pct = get_max_tp_for_timeframe(timeframe)

        # Get min R:R ratio from settings
        risk_cfg = (user_settings or {}).get("risk") or {}
        min_rr_ratio = safe_float(risk_cfg.get("min_risk_reward_ratio"), 1.5)

        levels = []
        remaining_pct = 100.0

        for price, qty_pct in raw_levels[:max_levels]:
            price = self._valid_take_profit(direction, entry, price, min_tp_pct=min_tp_pct, max_tp_pct=max_tp_pct)
            if not price:
                continue
            # Additional R:R validation if SL is provided
            if sl_price and entry > 0:
                tp_dist_pct = abs(price - entry) / entry * 100
                sl_dist_pct = abs(sl_price - entry) / entry * 100
                if sl_dist_pct > 0:
                    rr_ratio = tp_dist_pct / sl_dist_pct
                    if rr_ratio < min_rr_ratio:
                        logger.warning(
                            f"[Signal] TP at {price} has R:R {rr_ratio:.2f}:1, below minimum {min_rr_ratio}:1. "
                            f"Skipping this TP level."
                        )
                        continue
            qty = max(0.0, min(float(qty_pct or 0.0), remaining_pct))
            if qty <= 0:
                continue
            levels.append(TakeProfitLevel(price=round(price, 8), qty_pct=round(qty, 4)))
            remaining_pct -= qty
            if remaining_pct <= 0:
                break

        # BUG FIX: Sort TP levels by distance from entry (closest first).
        # For LONG: ascending price; for SHORT: descending price.
        # This ensures TP1 is always the nearest target.
        if levels:
            if direction == SignalDirection.LONG:
                levels.sort(key=lambda tp: tp.price)
            elif direction == SignalDirection.SHORT:
                levels.sort(key=lambda tp: tp.price, reverse=True)

        if not levels and raw_levels:
            fallback = self._valid_take_profit(direction, entry, raw_levels[0][0], min_tp_pct=min_tp_pct, max_tp_pct=max_tp_pct)
            if fallback:
                levels.append(TakeProfitLevel(price=round(fallback, 8), qty_pct=100.0))
        return levels

    @staticmethod
    def _max_tp_levels(user_settings: dict) -> int:
        tp_cfg = user_settings.get("take_profit") or {}
        return max(1, min(int(tp_cfg.get("num_levels") or settings.take_profit.num_levels or 1), 4))

    @staticmethod
    def _valid_stop_loss(
        direction: SignalDirection,
        entry: float,
        price: float | None,
        atr_pct: float = 0.0,
        user_settings: dict | None = None,
        timeframe: str = "60",
    ) -> float | None:
        """Validate stop loss with distance requirements.

        Checks:
        1. Basic direction (LONG: SL < entry, SHORT: SL > entry)
        2. Minimum distance (adjusts SL if too close)
        3. Maximum distance (rejects oversized risk)
        4. Not equal to entry price (would trigger immediately)

        If SL distance is below minimum, auto-adjusts to minimum distance
        instead of rejecting outright (to allow AI trades with tight stops).
        """
        try:
            value = float(price or 0)
            entry = float(entry or 0)
        except (TypeError, ValueError):
            return None
        if value <= 0 or entry <= 0:
            return None

        # Reject SL that equals entry (immediate trigger)
        if abs(value - entry) < entry * 0.0001:  # 0.01% tolerance
            logger.warning(f"[Signal] SL {value} too close to entry {entry}, rejecting")
            return None

        # Direction check
        if direction == SignalDirection.LONG and value >= entry:
            return None
        if direction == SignalDirection.SHORT and value <= entry:
            return None

        # Distance validation with timeframe-aware limits
        sl_dist_pct = abs(value - entry) / entry * 100
        min_sl_pct = SignalProcessor._get_min_sl_percentage(atr_pct, user_settings or {}, timeframe)
        max_sl_pct = SignalProcessor._get_max_sl_percentage(user_settings or {}, timeframe)

        # Auto-adjust if SL is too tight (don't reject outright)
        if sl_dist_pct < min_sl_pct:
            logger.warning(
                f"[Signal] SL distance {sl_dist_pct:.2f}% below minimum {min_sl_pct:.2f}%, "
                f"auto-adjusting SL to minimum distance"
            )
            # Adjust SL to minimum distance
            if direction == SignalDirection.LONG:
                value = entry * (1 - min_sl_pct / 100.0)
            else:
                value = entry * (1 + min_sl_pct / 100.0)
            sl_dist_pct = min_sl_pct

        if sl_dist_pct > max_sl_pct:
            logger.warning(
                f"[Signal] SL distance {sl_dist_pct:.2f}% above maximum {max_sl_pct:.2f}% "
                f"(entry={entry}, sl={value}), rejecting oversized risk"
            )
            return None

        return round(value, 8)

    @staticmethod
    def _valid_take_profit(
        direction: SignalDirection,
        entry: float,
        price: float | None,
        min_tp_pct: float = 0.0,
        max_tp_pct: float = 0.0,
    ) -> float | None:
        """Validate take profit with minimum/maximum distance requirement.

        Checks:
        1. Basic direction (LONG: TP > entry, SHORT: TP < entry)
        2. Minimum distance (auto-adjusts if too close)
        3. Maximum distance (warns if too far, but allows)

        If TP distance is below minimum, auto-adjusts to minimum distance
        instead of rejecting outright.
        """
        try:
            value = float(price or 0)
            entry = float(entry or 0)
        except (TypeError, ValueError):
            return None
        if value <= 0 or entry <= 0:
            return None

        # Direction check
        if direction == SignalDirection.LONG and value <= entry:
            return None
        if direction == SignalDirection.SHORT and value >= entry:
            return None

        # Minimum distance check - auto-adjust if too tight
        tp_dist_pct = abs(value - entry) / entry * 100
        if min_tp_pct <= 0:
            min_tp_pct = 0.3  # Default 0.3% minimum

        if max_tp_pct > 0 and tp_dist_pct > max_tp_pct:
            logger.warning(
                f"[Signal] TP distance {tp_dist_pct:.2f}% above suggested max {max_tp_pct:.2f}% "
                f"(entry={entry}, tp={value}), may be hard to reach for this timeframe"
            )

        if tp_dist_pct < min_tp_pct:
            logger.warning(
                f"[Signal] TP distance {tp_dist_pct:.2f}% below minimum {min_tp_pct:.2f}% "
                f"(entry={entry}, tp={value}), auto-adjusting to minimum distance"
            )
            # Adjust TP to minimum distance
            if direction == SignalDirection.LONG:
                value = entry * (1 + min_tp_pct / 100.0)
            else:
                value = entry * (1 - min_tp_pct / 100.0)

        return round(value, 8)

    @staticmethod
    def _get_min_sl_percentage(atr_pct: float, user_settings: dict, timeframe: str = "60") -> float:
        """Calculate minimum SL percentage based on ATR, config, and timeframe."""
        from timeframe_exits import get_min_sl_for_timeframe

        risk_cfg = user_settings.get("risk") or {}
        # Timeframe-based minimum (most important for realistic exits)
        tf_min = get_min_sl_for_timeframe(timeframe)
        # Config override can tighten but not loosen
        config_min = safe_float(risk_cfg.get("min_stop_loss_pct"), tf_min)
        # ATR-based minimum (dynamic volatility adjustment)
        atr_min = atr_pct * 1.2 if atr_pct > 0 else tf_min
        # Return the most restrictive minimum
        return max(tf_min, config_min, atr_min, 0.15)

    @staticmethod
    def _get_max_sl_percentage(user_settings: dict, timeframe: str = "60") -> float:
        """Maximum allowed SL percentage based on timeframe."""
        from timeframe_exits import get_max_sl_for_timeframe

        risk_cfg = user_settings.get("risk") or {}
        tf_max = get_max_sl_for_timeframe(timeframe)
        config_max = safe_float(risk_cfg.get("max_stop_loss_pct"), tf_max)
        # Use the more restrictive (smaller) max
        return min(tf_max, config_max)

    @staticmethod
    def _get_min_tp_percentage(atr_pct: float, user_settings: dict, timeframe: str = "60") -> float:
        """Calculate minimum TP percentage based on ATR, config, and timeframe."""
        from timeframe_exits import get_min_tp_for_timeframe

        tp_cfg = user_settings.get("take_profit") or {}
        tf_min = get_min_tp_for_timeframe(timeframe)
        config_min = safe_float(tp_cfg.get("min_tp_pct"), tf_min)
        atr_min = atr_pct * 0.8 if atr_pct > 0 else tf_min
        return max(tf_min, config_min, atr_min, 0.2)

    def _validate_risk_reward_ratio(
        self,
        entry: float,
        sl: float | None,
        tp_levels: list,
        direction: SignalDirection,
        user_settings: dict,
    ) -> tuple[bool, str]:
        """Validate that the trade has acceptable risk/reward ratio.

        Returns (is_valid, reason) tuple.
        Checks:
        - TP1 distance vs SL distance (minimum 1.5:1)
        - Average TP distance vs SL distance (minimum 1.2:1)
        """
        if not sl or not tp_levels or entry <= 0:
            return (False, "Missing SL or TP for R:R validation")

        sl_dist_pct = abs(sl - entry) / entry * 100
        if sl_dist_pct <= 0:
            return (False, "Invalid SL distance")

        risk_cfg = user_settings.get("risk") or {}
        min_rr_ratio = safe_float(risk_cfg.get("min_risk_reward_ratio"), 1.5)

        # Check TP1 (nearest target) R:R
        tp1_dist_pct = abs(tp_levels[0].price - entry) / entry * 100
        tp1_rr = tp1_dist_pct / sl_dist_pct

        if tp1_rr < min_rr_ratio:
            return (
                False,
                f"TP1 R:R ratio {tp1_rr:.2f}:1 below minimum {min_rr_ratio}:1 "
                f"(TP1={tp_levels[0].price}, SL={sl}, entry={entry})"
            )

        # Check average TP R:R (weighted by qty_pct)
        total_qty = sum(tp.qty_pct for tp in tp_levels)
        avg_rr = 0.0
        if total_qty > 0:
            avg_tp_dist = sum(
                abs(tp.price - entry) / entry * 100 * tp.qty_pct
                for tp in tp_levels
            ) / total_qty
            avg_rr = avg_tp_dist / sl_dist_pct
            min_avg_rr = safe_float(risk_cfg.get("min_avg_risk_reward_ratio"), 1.2)

            if avg_rr < min_avg_rr:
                return (
                    False,
                    f"Average TP R:R ratio {avg_rr:.2f}:1 below minimum {min_avg_rr}:1"
                )

        logger.info(
            f"[Signal] R:R validation passed: TP1={tp1_rr:.2f}:1, "
            f"avg={avg_rr:.2f}:1 (min={min_rr_ratio}:1)"
        )
        return (True, "")

    async def _check_correlation_risk(
        self,
        decision: TradeDecision,
        user_id: str | None,
        user_settings: dict | None = None,
    ) -> dict:
        """
        Check for correlation risk - too many positions in the same direction.

        Prevents over-concentration in one direction (e.g., multiple LONG positions)
        which would amplify losses if market moves against that direction.

        Returns dict with:
        - exceeded: bool - whether limit is exceeded
        - reason: str - explanation
        - current_exposure: dict - current position summary
        """
        result = {
            "exceeded": False,
            "reason": "",
            "current_exposure": {
                "long_positions": 0,
                "short_positions": 0,
                "long_notional_usdt": 0.0,
                "short_notional_usdt": 0.0,
            },
        }

        try:
            stmt = select(PositionModel).where(PositionModel.status.in_(["open", "pending"]))
            if user_id:
                stmt = stmt.where(PositionModel.user_id == user_id)

            db_result = await self.session.execute(stmt)
            positions = list(db_result.scalars().all())

            if not positions:
                return result

            # Count positions by direction
            long_positions = []
            short_positions = []

            for pos in positions:
                pos_dir = str(pos.direction or "long").lower()
                entry = safe_float(pos.entry_price)
                qty = safe_float(pos.remaining_quantity or pos.quantity)
                safe_float(pos.leverage, 1.0)

                notional = entry * qty if entry > 0 and qty > 0 else 0

                if pos_dir == "long":
                    long_positions.append({"ticker": pos.ticker, "notional": notional})
                elif pos_dir == "short":
                    short_positions.append({"ticker": pos.ticker, "notional": notional})

            result["current_exposure"]["long_positions"] = len(long_positions)
            result["current_exposure"]["short_positions"] = len(short_positions)
            result["current_exposure"]["long_notional_usdt"] = sum(p["notional"] for p in long_positions)
            result["current_exposure"]["short_notional_usdt"] = sum(p["notional"] for p in short_positions)

            # Get correlation limits from settings
            risk_cfg = (user_settings or {}).get("risk") or {}
            max_same_direction_positions = safe_int(
                first_valid(risk_cfg.get("max_same_direction_positions"), settings.risk.max_same_direction_positions),
                5,
            )
            max_correlated_pct = safe_float(
                first_valid(risk_cfg.get("max_correlated_exposure_pct"), settings.risk.max_correlated_exposure_pct),
                50.0,
            )

            # Check if new position would exceed limits
            new_direction = str(decision.direction.value or "long").lower()
            if new_direction == "long":
                current_count = len(long_positions)
                current_notional = sum(p["notional"] for p in long_positions)
            else:
                current_count = len(short_positions)
                current_notional = sum(p["notional"] for p in short_positions)

            # Position count limit
            if current_count >= max_same_direction_positions:
                result["exceeded"] = True
                result["reason"] = (
                    f"Correlation risk: {current_count} {new_direction} positions already open "
                    f"(max={max_same_direction_positions}). Adding more would over-concentrate risk."
                )
                logger.warning(f"[Signal] Correlation risk exceeded: {result['reason']}")
                return result

            # Notional exposure limit
            equity = float(self._resolved_risk_settings(user_settings).get("account_equity_usdt") or 1000)
            new_notional = decision.entry_price * decision.quantity if decision.entry_price and decision.quantity else 0
            total_notional_after = current_notional + new_notional
            exposure_pct = total_notional_after / equity * 100 if equity > 0 else 0

            if exposure_pct > max_correlated_pct:
                result["exceeded"] = True
                result["reason"] = (
                    f"Correlation risk: {new_direction} exposure would be {exposure_pct:.1f}% "
                    f"of equity (max={max_correlated_pct}%). "
                    f"Current={current_notional:.2f}USDT, New={new_notional:.2f}USDT, Equity={equity:.2f}USDT"
                )
                logger.warning(f"[Signal] Correlation risk exceeded: {result['reason']}")
                return result

            # Log correlation status
            logger.info(
                f"[Signal] Correlation check passed: {current_count + 1} {new_direction} positions "
                f"(exposure={exposure_pct:.1f}%, max={max_correlated_pct}%)"
            )

        except Exception as e:
            logger.warning(f"[Signal] Correlation check failed (allowing trade): {e}")

        return result

    @staticmethod
    def _normalize_size_pct(size_pct: float) -> float:
        """Normalize AI-returned size_pct to a 0-1 fraction.

        AI models may return either a 0-1 fraction or a 1-100 percentage.
        We detect which format was used and always return a 0-1 fraction.
        """
        value = float(size_pct or 0.0)
        if value <= 0:
            return 0.0
        # Values > 1 are treated as percentages (e.g. 50 means 50%)
        if value > 1.0:
            value = value / 100.0
        return max(0.0, min(value, 1.0))

    @staticmethod
    def _coerce_risk_float(value, default: float, min_value: float, max_value: float) -> float:
        if isinstance(value, bool):
            parsed = default
        elif isinstance(value, (int, float)):
            parsed = float(value)
        elif isinstance(value, str) and value.strip():
            try:
                parsed = float(value)
            except ValueError:
                parsed = default
        else:
            parsed = default
        return max(min_value, min(parsed, max_value))

    @classmethod
    def _resolved_risk_settings(cls, user_settings: dict | None = None) -> dict[str, float | str]:
        risk_cfg = (user_settings or {}).get("risk") or {}
        default_mode = str(getattr(settings.risk, "position_sizing_mode", "percentage") or "percentage").lower().strip()
        if default_mode not in {"percentage", "fixed", "risk_ratio"}:
            default_mode = "percentage"

        sizing_mode = str(risk_cfg.get("position_sizing_mode") or default_mode).lower().strip()
        if sizing_mode not in {"percentage", "fixed", "risk_ratio"}:
            sizing_mode = default_mode

        return {
            "account_equity_usdt": cls._coerce_risk_float(
                risk_cfg.get("account_equity_usdt"),
                cls._coerce_risk_float(getattr(settings.risk, "account_equity_usdt", 10000.0), 10000.0, 100.0, 10_000_000.0),
                100.0,
                10_000_000.0,
            ),
            "max_position_pct": cls._coerce_risk_float(
                risk_cfg.get("max_position_pct"),
                cls._coerce_risk_float(getattr(settings.risk, "max_position_pct", 10.0), 10.0, 0.1, 100.0),
                0.1,
                100.0,
            ),
            "fixed_position_size_usdt": cls._coerce_risk_float(
                risk_cfg.get("fixed_position_size_usdt"),
                cls._coerce_risk_float(getattr(settings.risk, "fixed_position_size_usdt", 100.0), 100.0, 1.0, 1_000_000.0),
                1.0,
                1_000_000.0,
            ),
            "risk_per_trade_pct": cls._coerce_risk_float(
                risk_cfg.get("risk_per_trade_pct"),
                cls._coerce_risk_float(getattr(settings.risk, "risk_per_trade_pct", 1.0), 1.0, 0.1, 100.0),
                0.1,
                100.0,
            ),
            "position_sizing_mode": sizing_mode,
        }

    def _calculate_position_size(
        self,
        price: float,
        size_pct: float,
        leverage: float,
        decision: TradeDecision | None = None,
        user_settings: dict | None = None,
    ) -> float:
        """Calculate position size based on account equity and risk.

        Supports three sizing modes:
        - percentage: AI suggests fraction of max_position_pct
        - fixed: Fixed USDT amount per trade
        - risk_ratio: Risk X% of account per trade (accounts for SL distance)

        NEW: Automatically respects exchange market limits (min/max amount, min/max cost).
        """
        risk_settings = self._resolved_risk_settings(user_settings)
        equity = float(risk_settings["account_equity_usdt"])
        max_position = float(risk_settings["max_position_pct"])
        sizing_mode = risk_settings["position_sizing_mode"]
        leverage = max(1.0, float(leverage or 1.0))

        size_fraction = self._normalize_size_pct(size_pct)

        if sizing_mode == "fixed":
            fixed_amount = float(risk_settings["fixed_position_size_usdt"])
            notional_value = fixed_amount * leverage
            logger.info(
                f"[PositionSize] Fixed mode: margin={fixed_amount}USDT, leverage={leverage}, notional={notional_value}USDT"
            )

        elif sizing_mode == "risk_ratio":
            risk_pct = float(risk_settings["risk_per_trade_pct"])
            sl_distance_pct = 0.0
            if decision and decision.stop_loss and self._has_valid_sl(price, decision.stop_loss):
                sl_distance_pct = self._sl_distance_pct(decision.direction, price, decision.stop_loss)

            if not sl_distance_pct or sl_distance_pct <= 0:
                logger.warning(
                    f"[PositionSize] risk_ratio mode requires valid stop loss, "
                    f"but SL distance is {sl_distance_pct}. Falling back to percentage mode."
                )
                margin_value = equity * (max_position / 100.0) * size_fraction
                notional_value = margin_value * leverage
            else:
                # Risk-based sizing: position size where hitting SL loses exactly risk_amount
                # NOT multiplied by leverage - leverage affects margin, not risk-based size
                risk_amount = equity * (risk_pct / 100.0)
                notional_value = risk_amount / (sl_distance_pct / 100.0)

                # Apply max_position_pct cap to prevent excessive positions
                max_notional = equity * (max_position / 100.0)
                if notional_value > max_notional:
                    original_notional = notional_value
                    notional_value = max_notional
                    logger.warning(
                        f"[PositionSize] risk_ratio exceeded max_position_pct ({max_position}%): "
                        f"calculated={original_notional:.2f}USDT, capped={max_notional:.2f}USDT"
                    )

                logger.info(
                    f"[PositionSize] risk_ratio mode: equity={equity}USDT, risk_pct={risk_pct}%, "
                    f"SL_distance={sl_distance_pct}% -> notional={notional_value}USDT (risk_amount={risk_amount}USDT)"
                )
        else:
            margin_value = equity * (max_position / 100.0) * size_fraction
            notional_value = margin_value * leverage

        # Calculate initial quantity
        if price <= 0:
            return 0.0

        quantity = notional_value / price

        # NEW: Apply exchange market limits
        if decision and decision.ticker:
            try:
                from exchange import adjust_quantity_for_limits, get_market_limits

                exchange_config = self._get_exchange_config(user_settings)
                exchange_id = exchange_config.get("exchange") or exchange_config.get("name") or settings.exchange.name
                market_type = exchange_config.get("market_type") or settings.exchange.market_type

                # Get market limits
                limits = get_market_limits(exchange_id, decision.ticker, market_type)

                if limits:
                    # Adjust quantity to respect limits
                    quantity = adjust_quantity_for_limits(quantity, price, limits)

                    # Log the adjustment
                    min_cost = limits.get("min_cost", 0)
                    max_cost = limits.get("max_cost", float("inf"))
                    if min_cost > 0 or max_cost < float("inf"):
                        final_cost = quantity * price
                        logger.info(
                            f"[PositionSize] Exchange limits applied: "
                            f"quantity={quantity:.6f}, cost={final_cost:.2f}USDT "
                            f"(min_cost={min_cost}, max_cost={max_cost})"
                        )
            except Exception as e:
                logger.warning(f"[PositionSize] Could not apply exchange limits: {e}")

        return float(round(quantity, 6))

    def _get_exchange_config(self, user_settings: dict | None = None) -> dict:
        """Get exchange configuration from settings."""
        config = {
            "exchange": settings.exchange.name,
            "market_type": settings.exchange.market_type,
        }
        if user_settings:
            user_exchange = user_settings.get("exchange") or {}
            config.update({
                "exchange": user_exchange.get("name") or user_exchange.get("exchange") or settings.exchange.name,
                "market_type": user_exchange.get("market_type") or settings.exchange.market_type,
            })
        return config

    def _has_valid_sl(self, entry_price: float, stop_loss: float | None = None) -> bool:
        """Check if we have valid stop loss info for risk-based sizing."""
        if not stop_loss or stop_loss <= 0 or entry_price <= 0:
            return False
        return True

    def _sl_distance_pct(self, direction, entry_price: float, stop_loss: float) -> float:
        """Calculate stop loss distance as percentage of entry price."""
        if entry_price <= 0 or stop_loss <= 0:
            return 0.0
        # BUG FIX: Use abs() to ensure we always return a positive distance.
        # A negative distance would invert position sizing calculations.
        if direction and str(direction).lower() == "short":
            return abs((stop_loss - entry_price) / entry_price) * 100.0
        return abs((entry_price - stop_loss) / entry_price) * 100.0

    async def _execute_trade(
        self,
        decision: TradeDecision,
        user_id: str | None,
        user_settings: dict | None = None,
    ) -> dict:
        """Execute the trade on the exchange."""
        exchange_config = {
            "exchange": settings.exchange.name,
            "api_key": settings.exchange.api_key,
            "api_secret": settings.exchange.api_secret,
            "password": settings.exchange.password,
            "live_trading": settings.exchange.live_trading,
            "sandbox_mode": settings.exchange.sandbox_mode,
            "market_type": settings.exchange.market_type,
            "default_order_type": settings.exchange.default_order_type,
            "stop_loss_order_type": settings.exchange.stop_loss_order_type,
            "limit_timeout_overrides": settings.exchange.limit_timeout_overrides,
        }
        if user_id:
            user = await get_user_by_id(self.session, user_id)
            if user:
                if user_settings is None:
                    user_settings = await self._load_user_settings(user_id)

                user_exchange = (user_settings or {}).get("exchange") or {}
                exchange_config.update({
                    "exchange": user_exchange.get("name") or user_exchange.get("exchange") or settings.exchange.name,
                    "api_key": user_exchange.get("api_key") if "api_key" in user_exchange else settings.exchange.api_key,
                    "api_secret": user_exchange.get("api_secret") if "api_secret" in user_exchange else settings.exchange.api_secret,
                    "password": user_exchange.get("password") if "password" in user_exchange else settings.exchange.password,
                    "live_trading": bool(user_exchange.get("live_trading", False)),
                    "sandbox_mode": bool(user_exchange.get("sandbox_mode", False)),
                    "market_type": user_exchange.get("market_type") or settings.exchange.market_type,
                    "default_order_type": user_exchange.get("default_order_type") or settings.exchange.default_order_type,
                    "stop_loss_order_type": user_exchange.get("stop_loss_order_type") or settings.exchange.stop_loss_order_type,
                    "limit_timeout_overrides": (
                        user_exchange.get("limit_timeout_overrides")
                        if "limit_timeout_overrides" in user_exchange
                        else settings.exchange.limit_timeout_overrides
                    ),
                    "max_leverage": user.max_leverage or 20,
                    "max_position_pct": user.max_position_pct or settings.risk.max_position_pct,
                })

                subscription = await get_user_active_subscription(self.session, user_id)
                if exchange_config["live_trading"] and (not user.live_trading_allowed or not subscription):
                    logger.warning(
                        f"[Signal] User {user_id} requested live trading without permission/subscription; using paper mode"
                    )
                    exchange_config["live_trading"] = False

        self._apply_position_limits(decision, exchange_config, user_settings)
        control_state = await trading_allowed(
            self.session,
            user_id=user_id,
            live_trading=bool(exchange_config.get("live_trading")),
        )
        if not control_state.get("allowed"):
            reason = control_state.get("block_reason") or "Trading is currently disabled"
            logger.warning(f"[Signal] Trade blocked by control mode: {reason}")
            return {
                "status": "rejected",
                "reason": reason,
                "trading_control": control_state,
            }

        raw_result = await execute_trade(decision, exchange_config)
        result: dict[str, object] = dict(raw_result) if isinstance(raw_result, dict) else {}
        order_status = str(result.get("status", "unknown"))

# Record trade
        signal_data = decision.signal.model_dump() if decision.signal else {}
        risk_cfg = (user_settings or {}).get("risk") or {}
        user_risk_profile = str(risk_cfg.get("ai_risk_profile") or settings.risk.ai_risk_profile)

        trade = await log_trade_db(
            session=self.session,
            user_id=user_id,
            ticker=decision.ticker,
            direction=decision.direction.value if decision.direction else "unknown",
            execute=decision.execute,
            order_status=order_status,
            pnl_pct=0.0,  # Will be updated on close
            payload={
                "signal": signal_data,
                "analysis": decision.ai_analysis.model_dump() if decision.ai_analysis else {},
                "result": result,
                "exchange_config": {
                    "exchange": exchange_config.get("exchange") or exchange_config.get("name"),
                    "live_trading": bool(exchange_config.get("live_trading")),
                    "sandbox_mode": bool(exchange_config.get("sandbox_mode")),
                },
                "strategy_name": signal_data.get("strategy", ""),
                "user_risk_profile": user_risk_profile,
            },
        )
        try:
            trade_payload = json.loads(str(trade.payload_json or "{}"))
        except (TypeError, json.JSONDecodeError):
            trade_payload = {}

        position_id = trade_payload.get("position_id")
        if position_id is not None:
            position_id = str(position_id)

        order_event = await record_order_event(
            session=self.session,
            decision=decision,
            result=result,
            user_id=user_id,
            trade_id=str(trade.id) if trade.id is not None else None,
            position_id=position_id,
        )
        result["order_event_id"] = order_event.id

        # Record metrics
        record_trade(
            decision.ticker,
            decision.direction.value if decision.direction else "unknown",
            order_status,
        )

        # Notify
        await notify_trade_executed(decision, result)

        return result

    async def _load_user_settings(self, user_id: str | None) -> dict:
        """Load decrypted per-user settings once for this webhook."""
        if not user_id:
            return {}
        user = await get_user_by_id(self.session, user_id)
        if not user:
            return {}
        try:
            raw_settings = json.loads(str(user.settings_json or "{}"))
            settings_data = decrypt_settings_payload(raw_settings)
            return dict(settings_data) if isinstance(settings_data, dict) else {}
        except Exception as exc:
            logger.warning(f"[Signal] Could not load user settings: {exc}")
            return {}

    def _apply_position_limits(
        self,
        decision: TradeDecision,
        exchange_config: dict,
        user_settings: dict | None = None,
    ) -> None:
        """Cap final quantity by the account and user max-position limits."""
        if not decision.entry_price or not decision.quantity or decision.quantity <= 0:
            return
        risk_settings = self._resolved_risk_settings(user_settings)
        sizing_mode = risk_settings.get("position_sizing_mode", "percentage")

        # Fixed mode: ensure quantity matches the configured fixed amount
        # Skip the max_position_pct limit since user explicitly set the amount
        if sizing_mode == "fixed":
            fixed_amount = float(risk_settings.get("fixed_position_size_usdt", 100.0))
            leverage = 1.0
            if decision.ai_analysis and decision.ai_analysis.recommended_leverage:
                leverage = max(1.0, float(decision.ai_analysis.recommended_leverage))
            expected_notional = fixed_amount * leverage
            current_notional = decision.quantity * decision.entry_price
            if abs(current_notional - expected_notional) > 1.0:
                logger.warning(
                    f"[Signal] Fixed mode: correcting notional from {current_notional:.2f}USDT "
                    f"to {expected_notional:.2f}USDT (margin={fixed_amount}USDT, leverage={leverage})"
                )
                decision.quantity = round(expected_notional / decision.entry_price, 6)
            return

        # Percentage/risk_ratio mode: apply max_position_pct limit
        account_equity = float(risk_settings["account_equity_usdt"])
        exchange_cap = self._coerce_risk_float(
            exchange_config.get("max_position_pct"),
            float(risk_settings["max_position_pct"]),
            0.1,
            100.0,
        )
        max_position_pct = min(exchange_cap, float(risk_settings["max_position_pct"]))
        max_leverage = max(1.0, min(float(exchange_config.get("max_leverage") or 125.0), 125.0))
        leverage = 1.0
        if decision.ai_analysis and decision.ai_analysis.recommended_leverage:
            leverage = max(1.0, min(float(decision.ai_analysis.recommended_leverage), max_leverage))
        max_notional = account_equity * (max_position_pct / 100.0) * leverage
        max_quantity = max_notional / float(decision.entry_price)
        if max_quantity > 0 and decision.quantity > max_quantity:
            logger.warning(
                f"[Signal] Quantity capped by max_position_pct: {decision.quantity} -> {max_quantity:.6f}"
            )
            decision.quantity = round(max_quantity, 6)

    async def _check_position_conflict(
        self,
        decision: TradeDecision,
        user_id: str | None,
        user_settings: dict | None = None,
    ) -> tuple[str | None, PositionModel | None]:
        """
        Check for conflicting open positions on the same ticker.
        Returns (rejection_reason, conflicting_position) tuple.

        Checks THREE layers:
        1. Pending orders of opposite direction (cancel them first)
        2. Database tracked OPEN positions (status="open", NOT pending)
        3. Exchange actual positions (for live trading)

        If opposite direction found, returns reason and the position to close.

        FIX: Pending orders should be cancelled before checking open positions.
        """
        try:
            direction = decision.direction.value if decision.direction else ""
            target_key = position_symbol_key(decision.ticker)

            # Step 1: Check for pending orders of opposite direction and cancel them
            pending_stmt = select(PositionModel).where(PositionModel.status == "pending")
            if user_id:
                pending_stmt = pending_stmt.where(PositionModel.user_id == user_id)

            pending_result = await self.session.execute(pending_stmt)
            pending_positions = [
                pos for pos in pending_result.scalars().all()
                if position_symbol_key(pos.ticker) == target_key
            ]

            # Cancel pending orders of opposite direction
            for pending_pos in pending_positions:
                pending_dir = (pending_pos.direction or "").lower()
                if (direction in ("long", "short") and pending_dir in ("long", "short")
                        and direction != pending_dir):
                    # Cancel this pending order
                    logger.info(
                        f"[Signal] Cancelling pending {pending_dir} order on {decision.ticker} "
                        f"(id={pending_pos.id[:8]}) before opening {direction}"
                    )
                    await self._cancel_pending_position(pending_pos, user_id, user_settings)

            # Step 2: Check database OPEN positions (FIX: only status="open")
            stmt = select(PositionModel).where(PositionModel.status == "open")
            if user_id:
                stmt = stmt.where(PositionModel.user_id == user_id)

            result = await self.session.execute(stmt)
            open_positions = [
                pos for pos in result.scalars().all()
                if position_symbol_key(pos.ticker) == target_key
            ]

            direction = decision.direction.value if decision.direction else ""

            # Check database positions for conflict
            for pos in open_positions:
                pos_dir = (pos.direction or "").lower()
                if (direction in ("long", "short") and pos_dir in ("long", "short")
                        and direction != pos_dir):
                    msg = (
                        f"Conflicting position: open {pos_dir} on {decision.ticker} "
                        f"(id={pos.id[:8]}). Closing existing position before opening {direction}."
                    )
                    logger.warning(f"[Signal] Database position conflict detected: {msg}")
                    return (msg, pos)

            # Step 2: Check exchange actual positions (for live trading)
            # This catches positions that might not be in database yet (concurrent signals)
            live_trading = False
            if user_settings:
                exchange_cfg = (user_settings or {}).get("exchange") or {}
                live_trading = bool(exchange_cfg.get("live_trading", False))

            if live_trading:
                from exchange import get_open_positions
                exchange_config = self._build_exchange_config(user_id, user_settings)
                try:
                    exchange_positions = await get_open_positions(exchange_config)
                    for ex_pos in exchange_positions:
                        ex_symbol = position_symbol_key(ex_pos.get("symbol") or "")
                        if ex_symbol != target_key:
                            continue
                        ex_side = str(ex_pos.get("side") or "").lower()
                        excontracts = safe_float(ex_pos.get("contracts") or ex_pos.get("contractSize") or 0)
                        if excontracts <= 0:
                            continue

                        # Map exchange side to position direction
                        ex_dir = "long" if ex_side in ("long", "buy") else "short"
                        if (direction in ("long", "short") and ex_dir in ("long", "short")
                                and direction != ex_dir):
                            msg = (
                                f"Exchange position conflict: {ex_dir} {excontracts} contracts on {decision.ticker}. "
                                f"Closing exchange position before opening {direction}."
                            )
                            logger.warning(f"[Signal] Exchange position conflict detected: {msg}")

                            # Create a synthetic position model for closing
                            # Use the first database position if exists, or create synthetic
                            for pos in open_positions:
                                if (pos.direction or "").lower() == ex_dir:
                                    return (msg, pos)

                            # No database position found, create synthetic position for closing
                            synthetic_pos = PositionModel(
                                id="exchange-sync",
                                ticker=decision.ticker,
                                direction=ex_dir,
                                quantity=excontracts,
                                entry_price=safe_float(ex_pos.get("entryPrice") or ex_pos.get("entry_price") or 0),
                                status="open",
                                live_trading=True,
                            )
                            return (msg, synthetic_pos)
                except Exception as ex:
                    logger.warning(f"[Signal] Failed to check exchange positions: {ex}")

            # Allow same-direction (scaling in)
            return (None, None)
        except Exception as e:
            logger.error(f"[Signal] Position conflict check failed: {e}")
            return (f"Position conflict check failed: {e}", None)

    async def _close_conflicting_position(
        self,
        position: PositionModel,
        user_id: str | None,
        user_settings: dict | None = None,
    ) -> dict:
        """
        Close an existing position and cancel its TP/SL orders.
        Used for reverse signal handling (close opposite position before opening new).

        Handles both:
        - Database tracked positions (with proper ID)
        - Synthetic positions from exchange (id="exchange-sync")
        """
        is_synthetic = position.id == "exchange-sync"
        result = {
            "status": "unknown",
            "ticker": position.ticker,
            "position_id": position.id[:8] if len(position.id) >= 8 else position.id,
            "is_synthetic": is_synthetic,
        }

        # Build exchange config
        exchange_config = self._build_exchange_config(user_id, user_settings)
        exchange_config["live_trading"] = position.live_trading

        try:
            # Step 1: Cancel TP orders (only for non-synthetic positions with order IDs)
            cancel_results = []
            if not is_synthetic and hasattr(position, "take_profit_order_ids_json"):
                tp_order_ids = loads_list(position.take_profit_order_ids_json)
                for order_id in tp_order_ids:
                    if order_id:
                        cancel_result = await cancel_order(str(order_id), position.ticker, exchange_config)
                        cancel_results.append(cancel_result)

            # Step 2: Cancel SL order (only for non-synthetic positions)
            if not is_synthetic and hasattr(position, "stop_loss_order_id") and position.stop_loss_order_id:
                await cancel_order(str(position.stop_loss_order_id), position.ticker, exchange_config)

            # Step 3: Close position on exchange (for live trading)
            exit_price = float(position.entry_price or 0)
            if position.live_trading and exchange_config.get("live_trading"):
                from exchange import get_ticker
                ticker_data = await get_ticker(position.ticker, exchange_config)
                exit_price = safe_float(ticker_data.get("last") or position.last_price or position.entry_price)

                # Build decision to close position
                close_qty = float(position.remaining_quantity or position.quantity or 0)
                if close_qty <= 0:
                    close_qty = float(position.quantity or 0)

                close_decision = TradeDecision(
                    ticker=position.ticker,
                    direction=SignalDirection.CLOSE_LONG if str(position.direction).lower() == "long" else SignalDirection.CLOSE_SHORT,
                    quantity=close_qty,
                    execute=True,
                )
                close_result = await execute_trade(close_decision, exchange_config)
                if close_result.get("status") == "closed":
                    exit_price = safe_float(close_result.get("exit_price") or exit_price)
                    result["exchange_close"] = close_result
                elif close_result.get("status") == "no_position":
                    logger.info(f"[Signal] Position already closed on exchange: {position.ticker}")
                    result["exchange_close"] = close_result
                else:
                    logger.warning(f"[Signal] Failed to close position on exchange: {close_result}")
                    result["exchange_close_error"] = close_result
                    if not is_synthetic:
                        result["status"] = "error"
                        result["reason"] = f"Failed to close on exchange: {close_result.get('reason')}"
                        return result

            # Step 4: Record close in database (only for non-synthetic positions)
            if not is_synthetic and exit_price > 0:
                try:
                    locked_result = await self.session.execute(
                        select(PositionModel)
                        .where(PositionModel.id == position.id)
                        .with_for_update()
                    )
                    locked_position = locked_result.scalar_one_or_none()
                    if locked_position and locked_position.status == "open":
                        await close_position_async(
                            session=self.session,
                            position=locked_position,
                            exit_price=exit_price,
                            close_reason="reverse_signal",
                        )
                        await self.session.flush()
                    elif locked_position and locked_position.status != "open":
                        logger.info(f"[Signal] Position {position.id[:8]} already closed by concurrent operation")
                except Exception as db_err:
                    logger.warning(f"[Signal] Failed to update database position: {db_err}")

            result["status"] = "closed"
            result["exit_price"] = exit_price
            result["cancelled_tp_orders"] = len([r for r in cancel_results if r.get("status") in ("cancelled", "simulated")])
            logger.info(
                f"[Signal] ✅ Closed conflicting position {position.id[:8] if len(position.id) >= 8 else position.id} "
                f"on {position.ticker} (exit={exit_price}, synthetic={is_synthetic})"
            )

        except Exception as e:
            logger.error(f"[Signal] Failed to close conflicting position: {e}")
            result["status"] = "error"
            result["reason"] = str(e)

        return result

    async def _cancel_pending_position(
        self,
        position: PositionModel,
        user_id: str | None,
        user_settings: dict | None = None,
    ) -> dict:
        """
        Cancel a pending position (limit order not yet filled).

        Used for reverse signal handling - cancel pending orders of opposite direction
        before opening new position.
        """
        result = {
            "status": "unknown",
            "ticker": position.ticker,
            "position_id": position.id[:8] if len(position.id) >= 8 else position.id,
        }

        try:
            # Build exchange config
            exchange_config = self._build_exchange_config(user_id, user_settings)
            exchange_config["live_trading"] = position.live_trading

            # Step 1: Cancel limit entry order on exchange
            if position.live_trading and position.entry_order_id:
                from exchange import cancel_order
                cancel_result = await cancel_order(str(position.entry_order_id), position.ticker, exchange_config)
                result["exchange_cancel"] = cancel_result

                if cancel_result.get("status") not in ("cancelled", "simulated"):
                    logger.warning(f"[Signal] Failed to cancel pending order on exchange: {cancel_result}")
                    result["status"] = "error"
                    result["reason"] = f"Exchange cancellation failed: {cancel_result.get('reason', 'unknown')}"
                    return result

            # Step 2: Cancel TP orders if any
            if hasattr(position, "take_profit_order_ids_json") and position.take_profit_order_ids_json:
                from exchange import cancel_order
                tp_order_ids = loads_list(position.take_profit_order_ids_json)
                for order_id in tp_order_ids:
                    if order_id:
                        await cancel_order(str(order_id), position.ticker, exchange_config)

            # Step 3: Cancel SL order if any
            if hasattr(position, "stop_loss_order_id") and position.stop_loss_order_id:
                from exchange import cancel_order
                await cancel_order(str(position.stop_loss_order_id), position.ticker, exchange_config)

            # Step 4: Mark position as cancelled in database
            position.status = "cancelled"
            position.closed_at = datetime.now(timezone.utc)
            position.close_reason = "cancelled_reverse_signal"
            await self.session.flush()

            result["status"] = "cancelled"
            logger.info(
                f"[Signal] ✅ Cancelled pending position {position.id[:8]} "
                f"on {position.ticker}"
            )

        except Exception as e:
            logger.error(f"[Signal] Failed to cancel pending position: {e}")
            result["status"] = "error"
            result["reason"] = str(e)
            return result

        return result

    def _build_exchange_config(self, user_id: str | None, user_settings: dict | None = None) -> dict:
        """Build exchange configuration from user settings or defaults."""
        exchange_config = {
            "exchange": settings.exchange.name,
            "api_key": settings.exchange.api_key,
            "api_secret": settings.exchange.api_secret,
            "password": settings.exchange.password,
            "live_trading": settings.exchange.live_trading,
            "sandbox_mode": settings.exchange.sandbox_mode,
            "market_type": settings.exchange.market_type,
            "default_order_type": settings.exchange.default_order_type,
            "stop_loss_order_type": settings.exchange.stop_loss_order_type,
            "limit_timeout_overrides": settings.exchange.limit_timeout_overrides,
        }

        if user_id and user_settings:
            user_exchange = (user_settings or {}).get("exchange") or {}
            exchange_config.update({
                "exchange": user_exchange.get("name") or user_exchange.get("exchange") or settings.exchange.name,
                "api_key": user_exchange.get("api_key") if "api_key" in user_exchange else settings.exchange.api_key,
                "api_secret": user_exchange.get("api_secret") if "api_secret" in user_exchange else settings.exchange.api_secret,
                "password": user_exchange.get("password") if "password" in user_exchange else settings.exchange.password,
                "live_trading": bool(user_exchange.get("live_trading", False)),
                "sandbox_mode": bool(user_exchange.get("sandbox_mode", False)),
                "market_type": user_exchange.get("market_type") or settings.exchange.market_type,
                "default_order_type": user_exchange.get("default_order_type") or settings.exchange.default_order_type,
                "stop_loss_order_type": user_exchange.get("stop_loss_order_type") or settings.exchange.stop_loss_order_type,
            })

        return exchange_config

    async def _record_and_notify_blocked(
        self,
        reservation,
        signal: TradingViewSignal,
        fingerprint: str,
        user_id: str | None,
        client_ip: str,
        reason: str,
        raw_body: dict | None = None,
    ):
        """Record and notify about blocked signal."""
        await notify_pre_filter_blocked(signal.ticker, signal.direction.value, reason)

        self._update_reserved_event(
            reservation,
            status="blocked",
            status_code=200,
            reason=reason,
            payload=raw_body or signal.model_dump(),
        )
