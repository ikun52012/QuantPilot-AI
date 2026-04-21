"""
Signal Server - Signal Processing Service
Handles the complete signal processing pipeline.
"""
import json
import hashlib
import secrets
import os
from datetime import datetime, timezone
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
    count_today_executed_trades,
)
from core.security import decrypt_settings_payload
from core.metrics import (
    record_signal_received,
    record_prefilter_result,
    record_ai_analysis,
    record_trade,
)
from core.cache import cached
from models import (
    TradingViewSignal,
    TradeDecision,
    SignalDirection,
    AIAnalysis,
    MarketContext,
)
from pre_filter import run_pre_filter, run_pre_filter_async
from ai_analyzer import analyze_signal
from market_data import fetch_market_context
from exchange import execute_trade
from notifier import (
    notify_signal_received,
    notify_pre_filter_blocked,
    notify_ai_analysis,
    notify_trade_executed,
    notify_error,
)


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
    """Verify webhook HMAC signature."""
    hmac_secret = os.getenv("WEBHOOK_HMAC_SECRET", "").strip()
    if not hmac_secret:
        return True  # No HMAC configured, allow

    if not signature:
        return False

    import hmac as hmac_module
    digest = hmac_module.new(hmac_secret.encode("utf-8"), request_body, hashlib.sha256).hexdigest()
    expected = f"sha256={digest}"

    return (
        hmac_module.compare_digest(signature, expected) or
        hmac_module.compare_digest(signature, digest)
    )


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
        start_time = datetime.now(timezone.utc)

        # Compute fingerprint for deduplication
        fingerprint = compute_webhook_fingerprint(raw_body or signal.model_dump(), user_id)

        # Check for duplicate
        if await has_recent_webhook_event(self.session, fingerprint, window_secs=300):
            logger.warning(f"[Signal] Duplicate webhook: {fingerprint[:16]}")
            return {"status": "duplicate", "reason": "Duplicate signal within 5 minutes"}

        # Record signal received
        record_signal_received(signal.ticker, signal.direction.value, user_id)

        # Notify signal received
        await notify_signal_received(signal.ticker, signal.direction.value, signal.price)

        try:
            # Step 1: Fetch market context
            market = await fetch_market_context(signal.ticker)

            # Step 2: Run pre-filter
            prefilter_result = await self._run_prefilter(signal, market, user_id)

            if not prefilter_result.passed:
                await self._record_and_notify_blocked(
                    signal, fingerprint, user_id, client_ip, prefilter_result.reason
                )
                return {
                    "status": "blocked",
                    "reason": prefilter_result.reason,
                    "checks": prefilter_result.checks,
                }

            # Step 3: AI Analysis
            analysis = await self._run_ai_analysis(signal, market)

            # Step 4: Build trade decision
            decision = self._build_trade_decision(signal, analysis, market, user_id)

            # Step 5: Execute trade
            if decision.execute:
                result = await self._execute_trade(decision, user_id)
            else:
                result = {"status": "rejected", "reason": decision.reason}

            # Record webhook event
            await record_webhook_event(
                session=self.session,
                user_id=user_id,
                fingerprint=fingerprint,
                ticker=signal.ticker,
                direction=signal.direction.value,
                status=result.get("status", "processed"),
                status_code=200,
                reason=result.get("reason", ""),
                client_ip=client_ip,
                payload=raw_body or signal.model_dump(),
            )

            return result

        except Exception as e:
            logger.error(f"[Signal] Processing error: {e}")
            await notify_error(str(e))

            # Record error
            await record_webhook_event(
                session=self.session,
                user_id=user_id,
                fingerprint=fingerprint,
                ticker=signal.ticker,
                direction=signal.direction.value,
                status="error",
                status_code=500,
                reason=str(e),
                client_ip=client_ip,
                payload=raw_body or signal.model_dump(),
            )

            return {"status": "error", "reason": str(e)}

    async def _run_prefilter(
        self,
        signal: TradingViewSignal,
        market: MarketContext,
        user_id: Optional[str],
    ) -> "PreFilterResult":
        """Run pre-filter checks."""
        # Get user settings for limits
        max_daily_trades = settings.risk.max_daily_trades
        max_daily_loss = settings.risk.max_daily_loss_pct

        if user_id:
            user = await get_user_by_id(self.session, user_id)
            if user:
                # Could load user-specific settings here
                pass

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

        # Set execute flag
        decision.execute = analysis.recommendation in ("execute", "modify")

        # Set stop loss
        if analysis.suggested_stop_loss:
            decision.stop_loss = analysis.suggested_stop_loss

        # Set take profit levels
        if analysis.suggested_tp1:
            from models import TakeProfitLevel
            levels = []
            if analysis.suggested_tp1:
                levels.append(TakeProfitLevel(price=analysis.suggested_tp1, qty_pct=analysis.tp1_qty_pct))
            if analysis.suggested_tp2:
                levels.append(TakeProfitLevel(price=analysis.suggested_tp2, qty_pct=analysis.tp2_qty_pct))
            if analysis.suggested_tp3:
                levels.append(TakeProfitLevel(price=analysis.suggested_tp3, qty_pct=analysis.tp3_qty_pct))
            if analysis.suggested_tp4:
                levels.append(TakeProfitLevel(price=analysis.suggested_tp4, qty_pct=analysis.tp4_qty_pct))
            decision.take_profit_levels = levels

        # Set trailing stop
        if settings.trailing_stop.mode != "none":
            from models import TrailingStopConfig, TrailingStopMode
            decision.trailing_stop = TrailingStopConfig(
                mode=TrailingStopMode(settings.trailing_stop.mode),
                trail_pct=settings.trailing_stop.trail_pct,
                activation_profit_pct=settings.trailing_stop.activation_profit_pct,
                trailing_step_pct=settings.trailing_stop.trailing_step_pct,
            )

        # Calculate position size
        decision.quantity = self._calculate_position_size(
            market.current_price,
            analysis.position_size_pct,
            analysis.recommended_leverage,
        )

        decision.reason = analysis.reasoning
        return decision

    def _calculate_position_size(
        self,
        price: float,
        size_pct: float,
        leverage: float,
    ) -> float:
        """Calculate position size based on account equity and risk."""
        equity = settings.risk.account_equity_usdt
        max_position = settings.risk.max_position_pct

        # AI returns 0..1 as a fraction of the configured maximum position.
        # Accept >1 as a legacy percentage for backward compatibility.
        size_fraction = max(0.0, min(float(size_pct or 0.0), 1.0))
        if size_pct and size_pct > 1:
            size_fraction = max(0.0, min(float(size_pct) / 100.0, 1.0))
        leverage = max(1.0, float(leverage or 1.0))
        margin_value = equity * (max_position / 100.0) * size_fraction
        notional_value = margin_value * leverage

        # Calculate quantity
        if price > 0:
            quantity = notional_value / price
            return round(quantity, 6)

        return 0.0

    async def _execute_trade(
        self,
        decision: TradeDecision,
        user_id: Optional[str],
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
                user_settings = {}
                try:
                    raw_settings = json.loads(user.settings_json or "{}")
                    user_settings = decrypt_settings_payload(raw_settings)
                except Exception as exc:
                    logger.warning(f"[Signal] Could not load user exchange settings: {exc}")

                user_exchange = user_settings.get("exchange") or {}
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
        result = await execute_trade(decision, exchange_config)

        # Record trade
        await log_trade_db(
            session=self.session,
            user_id=user_id,
            ticker=decision.ticker,
            direction=decision.direction.value if decision.direction else "unknown",
            execute=decision.execute,
            order_status=result.get("status", "unknown"),
            pnl_pct=0.0,  # Will be updated on close
            payload={
                "signal": decision.signal.model_dump() if decision.signal else {},
                "analysis": decision.ai_analysis.model_dump() if decision.ai_analysis else {},
                "result": result,
                "exchange_config": {
                    "exchange": exchange_config.get("exchange") or exchange_config.get("name"),
                    "live_trading": bool(exchange_config.get("live_trading")),
                    "sandbox_mode": bool(exchange_config.get("sandbox_mode")),
                },
            },
        )

        # Record metrics
        record_trade(
            decision.ticker,
            decision.direction.value if decision.direction else "unknown",
            result.get("status", "unknown"),
        )

        # Notify
        await notify_trade_executed(decision, result)

        return result

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

    async def _record_and_notify_blocked(
        self,
        signal: TradingViewSignal,
        fingerprint: str,
        user_id: Optional[str],
        client_ip: str,
        reason: str,
    ):
        """Record and notify about blocked signal."""
        await notify_pre_filter_blocked(signal.ticker, signal.direction.value, reason)

        await record_webhook_event(
            session=self.session,
            user_id=user_id,
            fingerprint=fingerprint,
            ticker=signal.ticker,
            direction=signal.direction.value,
            status="blocked",
            status_code=200,
            reason=reason,
            client_ip=client_ip,
            payload=signal.model_dump(),
        )
