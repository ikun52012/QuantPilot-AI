"""
Signal Server - Signal Processing Service
Handles the complete signal processing pipeline.
"""
import json
import hashlib
import os
from typing import Optional
import asyncio

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from core.config import settings
from core.database import (
    record_webhook_event,
    has_recent_webhook_event,
    get_user_by_id,
    get_user_active_subscription,
    log_trade_db,
)
from core.security import decrypt_settings_payload
from core.metrics import (
    record_signal_received,
    record_prefilter_result,
    record_ai_analysis,
    record_trade,
)
from core.trading_control import trading_allowed
from services.order_reconciler import record_order_event
from models import (
    TradingViewSignal,
    TradeDecision,
    SignalDirection,
    AIAnalysis,
    MarketContext,
)
from pre_filter import run_pre_filter_async
from ai_analyzer import analyze_signal
from market_data import fetch_market_context, fetch_enhanced_market_context
from exchange import execute_trade
from notifier import (
    notify_signal_received,
    notify_pre_filter_blocked,
    notify_ai_analysis,
    notify_trade_executed,
    notify_error,
)


_WEBHOOK_LOCKS: dict[str, asyncio.Lock] = {}
_WEBHOOK_LOCKS_GUARD = asyncio.Lock()
_SENSITIVE_EVENT_KEY_PARTS = ("secret", "token", "password", "api_key", "api_secret")


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

def compute_webhook_fingerprint(body: dict, user_id: Optional[str] = None) -> str:
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
# Webhook Signature Verification
# ─────────────────────────────────────────────

def verify_webhook_signature(request_body: bytes, signature: str) -> bool:
    """
    Verify webhook HMAC signature.

    Security policy:
    - If WEBHOOK_HMAC_SECRET is configured: signature MUST be valid
    - If not configured: allow but log warning (for development/testing)
    - In production (LIVE_TRADING=true): signature is REQUIRED
    """
    hmac_secret = settings.webhook_hmac_secret.strip()

    # Production mode: always require HMAC
    if settings.exchange.live_trading and not hmac_secret:
        logger.error("[Security] LIVE_TRADING enabled but WEBHOOK_HMAC_SECRET not set!")
        return False

    # Development mode: allow without HMAC but warn
    if not hmac_secret:
        logger.warning(
            "[Security] Webhook HMAC not configured. "
            "Set WEBHOOK_HMAC_SECRET for production security."
        )
        return True

    if not signature:
        logger.warning("[Security] Webhook request missing signature")
        return False

    import hmac as hmac_module
    digest = hmac_module.new(hmac_secret.encode("utf-8"), request_body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"

    valid = (
        hmac_module.compare_digest(signature, expected) or
        hmac_module.compare_digest(signature, digest)
    )

    if not valid:
        logger.warning("[Security] Webhook signature verification failed")

    return valid


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
        user_id: Optional[str] = None,
        client_ip: str = "",
        raw_body: dict = None,
    ) -> dict:
        """
        Process a webhook signal through the complete pipeline.
        Returns the result of the processing.
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
            # Step 1: Fetch market context
            enhanced_filters = settings.ai.voting_enabled or os.getenv("ENHANCED_FILTERS_ENABLED", "true").lower() == "true"
            if enhanced_filters:
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
            analysis = await self._run_ai_analysis(signal, market)

            # Step 4: Build trade decision
            decision = self._build_trade_decision(signal, analysis, market, user_id, user_settings)

            # Step 5: Check for conflicting open positions
            if decision.execute:
                conflict = await self._check_position_conflict(decision, user_id)
                if conflict:
                    decision.execute = False
                    decision.reason = conflict

            # Step 6: Execute trade
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
        user_id: Optional[str],
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
        user_id: Optional[str],
        user_settings: Optional[dict] = None,
    ) -> "PreFilterResult":
        """Run pre-filter checks."""
        # Get user settings for limits
        max_daily_trades = settings.risk.max_daily_trades
        max_daily_loss = settings.risk.max_daily_loss_pct

        user_risk = (user_settings or {}).get("risk") or {}
        if user_risk:
            max_daily_trades = int(user_risk.get("max_daily_trades") or max_daily_trades)
            max_daily_loss = float(user_risk.get("max_daily_loss_pct") or max_daily_loss)

        result = await run_pre_filter_async(
            signal=signal,
            market=market,
            max_daily_trades=max_daily_trades,
            max_daily_loss_pct=max_daily_loss,
            user_id=user_id,
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
    ) -> AIAnalysis:
        """Run AI analysis on the signal."""
        import time
        start = time.time()

        analysis = await analyze_signal(signal, market)

        latency = time.time() - start
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
        user_id: Optional[str],
        user_settings: Optional[dict] = None,
    ) -> TradeDecision:
        """Build trade decision from signal and analysis."""
        decision = TradeDecision(
            signal=signal,
            ai_analysis=analysis,
            ticker=signal.ticker,
            direction=signal.direction,
            entry_price=signal.price,
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
        if decision.execute and analysis.recommendation == "modify" and analysis.suggested_entry:
            suggested = float(analysis.suggested_entry)
            if suggested > 0:
                price_diff_pct = abs(suggested - signal.price) / signal.price * 100 if signal.price > 0 else 0
                # Only accept modified entry if it's within 5% of signal price
                # (prevents AI from suggesting wildly different prices)
                if price_diff_pct <= 5.0:
                    logger.info(
                        f"[Signal] AI modified entry: {signal.price} → {suggested} "
                        f"({price_diff_pct:+.2f}% adjustment via SMC/FVG)"
                    )
                    decision.entry_price = suggested
                else:
                    logger.warning(
                        f"[Signal] AI suggested entry {suggested} is {price_diff_pct:.2f}% "
                        f"from signal price {signal.price}; using original price"
                    )

        if decision.execute:
            self._apply_exit_plan(decision, signal, analysis, user_settings or {})
            if signal.direction in {SignalDirection.LONG, SignalDirection.SHORT}:
                if not decision.stop_loss:
                    decision.execute = False
                    decision.reason = "No valid stop loss available for opening trade"
                    return decision
                if not decision.take_profit_levels:
                    decision.execute = False
                    decision.reason = "No valid take-profit target available for opening trade"
                    return decision

        # Set trailing stop
        trailing_cfg = (user_settings or {}).get("trailing_stop") or {}
        trailing_mode = str(trailing_cfg.get("mode") or settings.trailing_stop.mode)
        if trailing_mode != "none":
            from models import TrailingStopConfig, TrailingStopMode
            decision.trailing_stop = TrailingStopConfig(
                mode=TrailingStopMode(trailing_mode),
                trail_pct=float(trailing_cfg.get("trail_pct") or settings.trailing_stop.trail_pct),
                activation_profit_pct=float(trailing_cfg.get("activation_profit_pct") or settings.trailing_stop.activation_profit_pct),
                trailing_step_pct=float(trailing_cfg.get("trailing_step_pct") or settings.trailing_stop.trailing_step_pct),
            )

        # Calculate position size
        decision.quantity = self._calculate_position_size(
            market.current_price or signal.price,
            analysis.position_size_pct,
            analysis.recommended_leverage,
            decision=decision,
        )

        decision.reason = analysis.reasoning
        return decision

    def _apply_exit_plan(
        self,
        decision: TradeDecision,
        signal: TradingViewSignal,
        analysis: AIAnalysis,
        user_settings: dict,
    ) -> None:
        """Apply either custom configured exits or validated AI-generated exits."""
        if signal.direction not in {SignalDirection.LONG, SignalDirection.SHORT}:
            return

        risk_cfg = user_settings.get("risk") or {}
        exit_mode = str(risk_cfg.get("exit_management_mode") or settings.risk.exit_management_mode)
        if exit_mode == "custom":
            self._apply_custom_exit_plan(decision, signal, user_settings)
            return

        decision.stop_loss = self._valid_stop_loss(signal.direction, signal.price, analysis.suggested_stop_loss)

        raw_levels = [
            (analysis.suggested_tp1, analysis.tp1_qty_pct),
            (analysis.suggested_tp2, analysis.tp2_qty_pct),
            (analysis.suggested_tp3, analysis.tp3_qty_pct),
            (analysis.suggested_tp4, analysis.tp4_qty_pct),
        ]
        max_levels = self._max_tp_levels(user_settings)
        decision.take_profit_levels = self._build_take_profit_levels(signal.direction, signal.price, raw_levels, max_levels)
        if decision.take_profit_levels:
            decision.take_profit = decision.take_profit_levels[0].price

    def _apply_custom_exit_plan(self, decision: TradeDecision, signal: TradingViewSignal, user_settings: dict) -> None:
        """Build fixed percentage SL/TP exits from admin configuration."""
        entry = float(signal.price or 0)
        if entry <= 0:
            return

        risk_cfg = user_settings.get("risk") or {}
        tp_cfg = user_settings.get("take_profit") or {}
        stop_pct = max(0.01, float(risk_cfg.get("custom_stop_loss_pct") or settings.risk.custom_stop_loss_pct or 0))
        tp1_pct = float(tp_cfg.get("tp1_pct") or settings.take_profit.tp1_pct)
        tp2_pct = float(tp_cfg.get("tp2_pct") or settings.take_profit.tp2_pct)
        tp3_pct = float(tp_cfg.get("tp3_pct") or settings.take_profit.tp3_pct)
        tp4_pct = float(tp_cfg.get("tp4_pct") or settings.take_profit.tp4_pct)
        tp1_qty = float(tp_cfg.get("tp1_qty") or settings.take_profit.tp1_qty)
        tp2_qty = float(tp_cfg.get("tp2_qty") or settings.take_profit.tp2_qty)
        tp3_qty = float(tp_cfg.get("tp3_qty") or settings.take_profit.tp3_qty)
        tp4_qty = float(tp_cfg.get("tp4_qty") or settings.take_profit.tp4_qty)

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
        )
        if decision.take_profit_levels:
            decision.take_profit = decision.take_profit_levels[0].price

    def _build_take_profit_levels(
        self,
        direction: SignalDirection,
        entry: float,
        raw_levels: list[tuple[Optional[float], float]],
        max_levels: int,
    ) -> list:
        """Validate TP direction and cap cumulative close quantity to 100%."""
        from models import TakeProfitLevel

        levels = []
        remaining_pct = 100.0
        for price, qty_pct in raw_levels[:max_levels]:
            price = self._valid_take_profit(direction, entry, price)
            if not price:
                continue
            qty = max(0.0, min(float(qty_pct or 0.0), remaining_pct))
            if qty <= 0:
                continue
            levels.append(TakeProfitLevel(price=round(price, 8), qty_pct=round(qty, 4)))
            remaining_pct -= qty
            if remaining_pct <= 0:
                break

        if not levels and raw_levels:
            fallback = self._valid_take_profit(direction, entry, raw_levels[0][0])
            if fallback:
                levels.append(TakeProfitLevel(price=round(fallback, 8), qty_pct=100.0))
        return levels

    @staticmethod
    def _max_tp_levels(user_settings: dict) -> int:
        tp_cfg = user_settings.get("take_profit") or {}
        return max(1, min(int(tp_cfg.get("num_levels") or settings.take_profit.num_levels or 1), 4))

    @staticmethod
    def _valid_stop_loss(direction: SignalDirection, entry: float, price: Optional[float]) -> Optional[float]:
        try:
            value = float(price or 0)
            entry = float(entry or 0)
        except (TypeError, ValueError):
            return None
        if value <= 0 or entry <= 0:
            return None
        if direction == SignalDirection.LONG and value < entry:
            return value
        if direction == SignalDirection.SHORT and value > entry:
            return value
        return None

    @staticmethod
    def _valid_take_profit(direction: SignalDirection, entry: float, price: Optional[float]) -> Optional[float]:
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

    def _calculate_position_size(
        self,
        price: float,
        size_pct: float,
        leverage: float,
        decision: Optional[TradeDecision] = None,
    ) -> float:
        """Calculate position size based on account equity and risk.

        Supports three sizing modes:
        - percentage: AI suggests fraction of max_position_pct
        - fixed: Fixed USDT amount per trade
        - risk_ratio: Risk X% of account per trade (accounts for SL distance)
        """
        equity = settings.risk.account_equity_usdt
        max_position = settings.risk.max_position_pct
        sizing_mode = settings.risk.position_sizing_mode
        leverage = max(1.0, float(leverage or 1.0))

        if sizing_mode == "fixed":
            # Fixed USDT amount per trade
            fixed_amount = settings.risk.fixed_position_size_usdt
            # Apply AI size_pct as a multiplier (0-1 or percentage)
            size_fraction = max(0.0, min(float(size_pct or 0.0), 1.0))
            if size_pct and size_pct > 1:
                size_fraction = max(0.0, min(float(size_pct) / 100.0, 1.0))
            notional_value = fixed_amount * leverage * size_fraction

        elif sizing_mode == "risk_ratio":
            # Risk X% of account per trade
            risk_pct = settings.risk.risk_per_trade_pct
            # Calculate stop loss distance from decision
            sl_distance_pct = 0.0
            if decision and decision.stop_loss and self._has_valid_sl(price, decision.stop_loss):
                sl_distance_pct = self._sl_distance_pct(decision.direction, price, decision.stop_loss)
            if not sl_distance_pct:
                sl_distance_pct = 2.0  # Default 2% SL distance as fallback
            # Position size = (account * risk_pct) / (sl_distance * leverage)
            risk_amount = equity * (risk_pct / 100.0)
            notional_value = (risk_amount / (sl_distance_pct / 100.0)) * leverage
        else:
            # Default: percentage mode
            size_fraction = max(0.0, min(float(size_pct or 0.0), 1.0))
            if size_pct and size_pct > 1:
                size_fraction = max(0.0, min(float(size_pct) / 100.0, 1.0))
            margin_value = equity * (max_position / 100.0) * size_fraction
            notional_value = margin_value * leverage

        # Calculate quantity
        if price > 0:
            quantity = notional_value / price
            return round(quantity, 6)

        return 0.0

    def _has_valid_sl(self, entry_price: float, stop_loss: Optional[float] = None) -> bool:
        """Check if we have valid stop loss info for risk-based sizing."""
        if not stop_loss or stop_loss <= 0 or entry_price <= 0:
            return False
        return True

    def _sl_distance_pct(self, direction, entry_price: float, stop_loss: float) -> float:
        """Calculate stop loss distance as percentage of entry price."""
        if entry_price <= 0 or stop_loss <= 0:
            return 0.0
        if direction and str(direction).lower() == "short":
            return ((stop_loss - entry_price) / entry_price) * 100.0
        return ((entry_price - stop_loss) / entry_price) * 100.0

    async def _execute_trade(
        self,
        decision: TradeDecision,
        user_id: Optional[str],
        user_settings: Optional[dict] = None,
    ) -> dict:
        """Execute the trade on the exchange."""
        exchange_config = {
            "exchange": settings.exchange.name,
            "api_key": settings.exchange.api_key,
            "api_secret": settings.exchange.api_secret,
            "password": settings.exchange.password,
            "live_trading": settings.exchange.live_trading,
            "sandbox_mode": settings.exchange.sandbox_mode,
        }
        if user_id:
            user = await get_user_by_id(self.session, user_id)
            if user:
                if user_settings is None:
                    user_settings = await self._load_user_settings(user_id)

                user_exchange = (user_settings or {}).get("exchange") or {}
                exchange_config.update({
                    "exchange": user_exchange.get("name") or user_exchange.get("exchange") or settings.exchange.name,
                    "api_key": user_exchange.get("api_key") or settings.exchange.api_key,
                    "api_secret": user_exchange.get("api_secret") or settings.exchange.api_secret,
                    "password": user_exchange.get("password") or settings.exchange.password,
                    "live_trading": bool(user_exchange.get("live_trading", False)),
                    "sandbox_mode": bool(user_exchange.get("sandbox_mode", False)),
                    "max_leverage": user.max_leverage or 20,
                    "max_position_pct": user.max_position_pct or settings.risk.max_position_pct,
                })

                subscription = await get_user_active_subscription(self.session, user_id)
                if exchange_config["live_trading"] and (not user.live_trading_allowed or not subscription):
                    logger.warning(
                        f"[Signal] User {user_id} requested live trading without permission/subscription; using paper mode"
                    )
                    exchange_config["live_trading"] = False

        self._apply_position_limits(decision, exchange_config)
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

        result = await execute_trade(decision, exchange_config)

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
            order_status=result.get("status", "unknown"),
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
            trade_payload = json.loads(trade.payload_json or "{}")
        except (TypeError, json.JSONDecodeError):
            trade_payload = {}

        order_event = await record_order_event(
            session=self.session,
            decision=decision,
            result=result,
            user_id=user_id,
            trade_id=trade.id,
            position_id=trade_payload.get("position_id"),
        )
        result["order_event_id"] = order_event.id

        # Record metrics
        record_trade(
            decision.ticker,
            decision.direction.value if decision.direction else "unknown",
            result.get("status", "unknown"),
        )

        # Notify
        await notify_trade_executed(decision, result)

        return result

    async def _load_user_settings(self, user_id: Optional[str]) -> dict:
        """Load decrypted per-user settings once for this webhook."""
        if not user_id:
            return {}
        user = await get_user_by_id(self.session, user_id)
        if not user:
            return {}
        try:
            raw_settings = json.loads(user.settings_json or "{}")
            settings_data = decrypt_settings_payload(raw_settings)
            return settings_data if isinstance(settings_data, dict) else {}
        except Exception as exc:
            logger.warning(f"[Signal] Could not load user settings: {exc}")
            return {}

    def _apply_position_limits(self, decision: TradeDecision, exchange_config: dict) -> None:
        """Cap final quantity by the account and user max-position limits."""
        if not decision.entry_price or not decision.quantity or decision.quantity <= 0:
            return
        max_position_pct = float(exchange_config.get("max_position_pct") or settings.risk.max_position_pct)
        max_leverage = max(1.0, min(float(exchange_config.get("max_leverage") or 125.0), 125.0))
        leverage = 1.0
        if decision.ai_analysis and decision.ai_analysis.recommended_leverage:
            leverage = max(1.0, min(float(decision.ai_analysis.recommended_leverage), max_leverage))
        max_notional = settings.risk.account_equity_usdt * (max_position_pct / 100.0) * leverage
        max_quantity = max_notional / float(decision.entry_price)
        if max_quantity > 0 and decision.quantity > max_quantity:
            logger.warning(
                f"[Signal] Quantity capped by max_position_pct: {decision.quantity} -> {max_quantity:.6f}"
            )
            decision.quantity = round(max_quantity, 6)

    async def _check_position_conflict(
        self,
        decision: TradeDecision,
        user_id: Optional[str],
    ) -> Optional[str]:
        """
        Check for conflicting open positions on the same ticker.
        Returns a rejection reason string, or None if no conflict.
        """
        from sqlalchemy import select
        from core.database import PositionModel

        try:
            stmt = select(PositionModel).where(
                PositionModel.status == "open",
                PositionModel.ticker == decision.ticker,
            )
            if user_id:
                stmt = stmt.where(PositionModel.user_id == user_id)

            result = await self.session.execute(stmt)
            open_positions = list(result.scalars().all())

            if not open_positions:
                return None

            direction = decision.direction.value if decision.direction else ""
            for pos in open_positions:
                pos_dir = (pos.direction or "").lower()
                # Block opposite direction on same ticker
                if (direction in ("long", "short") and pos_dir in ("long", "short")
                        and direction != pos_dir):
                    msg = (
                        f"Conflicting position: open {pos_dir} on {decision.ticker} "
                        f"(id={pos.id[:8]}). Close it before opening {direction}."
                    )
                    logger.warning(f"[Signal] Position conflict: {msg}")
                    return msg

            # Allow same-direction (scaling in)
            return None
        except Exception as e:
            logger.warning(f"[Signal] Position conflict check failed (allowing trade): {e}")
            return None

    async def _record_and_notify_blocked(
        self,
        reservation,
        signal: TradingViewSignal,
        fingerprint: str,
        user_id: Optional[str],
        client_ip: str,
        reason: str,
        raw_body: Optional[dict] = None,
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
