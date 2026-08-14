from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine, inspect, select

from ai_analyzer import _price_to_bucket
from core.ai_cost_tracker import (
    AIBudgetExceeded,
    AICostTracker,
    reset_ai_usage_context,
    set_ai_usage_context,
)
from core.config import settings
from core.database import (
    Base,
    DatabaseManager,
    PositionModel,
    WebhookEventModel,
    has_recent_webhook_event,
    record_scanner_audit,
)
from core.portfolio_risk import check_pre_trade_var
from core.utils.datetime import utcnow
from models import (
    AIAnalysis,
    MarketContext,
    PreFilterResult,
    SignalDirection,
    TradeDecision,
    TradingViewSignal,
)
from routers import webhook as webhook_module
from services.order_reconciler import record_order_event
from services.scanner_learning import _best_threshold
from services.signal_processor import SignalProcessor, compute_webhook_fingerprint
from services.unified_ohlcv import NormalizedCandle, UnifiedOHLCVProvider
from services.webhook_worker import _retryable_result, process_webhook_event


def test_runtime_schema_upgrade_adds_queue_fields_to_webhook_table_only():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE webhook_events")
            connection.exec_driver_sql(
                "CREATE TABLE webhook_events ("
                "id VARCHAR(36) PRIMARY KEY, "
                "fingerprint VARCHAR(64) NOT NULL, "
                "status VARCHAR(20) NOT NULL, "
                "created_at TIMESTAMP"
                ")"
            )
            DatabaseManager._ensure_schema(connection)

            inspector = inspect(connection)
            webhook_columns = {
                column["name"] for column in inspector.get_columns("webhook_events")
            }
            scanner_columns = {
                column["name"] for column in inspector.get_columns("scanner_audits")
            }
            assert {"updated_at", "attempt_count", "next_attempt_at"} <= webhook_columns
            assert not ({"attempt_count", "next_attempt_at"} & scanner_columns)
    finally:
        engine.dispose()


def test_successful_exchange_result_with_ai_fallback_is_not_retried():
    result = {
        "status": "filled",
        "analysis": {"warnings": ["AI fallback triggered: provider timeout"]},
    }
    assert _retryable_result(result) is False
    assert _retryable_result(
        {
            "status": "rejected",
            "analysis": {"warnings": ["AI fallback triggered: provider timeout"]},
        }
    ) is False
    assert _retryable_result(
        {
            "status": "rejected",
            "reason": "queue full",
            "retry_safe": True,
            "failure_stage": "pre_execution",
        }
    ) is True


@pytest.mark.asyncio
async def test_live_entry_is_held_when_ai_uses_local_fallback(monkeypatch):
    from services import signal_processor as signal_processor_module

    monkeypatch.setattr(settings.exchange, "live_trading", True)
    monkeypatch.setattr(
        signal_processor_module,
        "trading_allowed",
        AsyncMock(return_value={"allowed": True, "mode": "enabled"}),
    )
    monkeypatch.setattr(signal_processor_module, "record_signal_received", lambda *args, **kwargs: None)
    monkeypatch.setattr(signal_processor_module, "notify_signal_received", AsyncMock())

    processor = SignalProcessor(AsyncMock())
    reservation = SimpleNamespace(
        status="received",
        status_code=202,
        reason="reserved",
        payload_json="{}",
        updated_at=None,
    )
    processor._reserve_webhook_event = AsyncMock(return_value=reservation)
    processor._checkpoint_db = AsyncMock()
    processor._record_signal_audit = AsyncMock()
    processor._run_prefilter = AsyncMock(
        return_value=PreFilterResult(passed=True, reason="passed", checks={})
    )
    processor._run_ai_analysis = AsyncMock(
        return_value=AIAnalysis(
            confidence=0.9,
            recommendation="execute",
            reasoning="Local rules approved the signal",
            warnings=["AI fallback triggered: provider unavailable"],
        )
    )
    processor._build_trade_decision = lambda *args, **kwargs: pytest.fail(
        "live fallback must be held before a trade decision is built"
    )
    signal = TradingViewSignal(
        ticker="BTCUSDT",
        direction=SignalDirection.LONG,
        price=50_000,
        timeframe="60",
    )
    market = MarketContext(ticker="BTCUSDT", current_price=50_000, volume_24h=1_000_000)

    result = await processor._process_signal_locked(
        signal,
        raw_body=signal.model_dump(),
        prefetched_market=market,
        loaded_user_settings={},
    )

    assert result["status"] == "held"
    assert result["ai_fallback"] is True
    assert reservation.status == "held"


def test_portfolio_var_scales_with_leveraged_gross_exposure():
    prices = [100.0]
    for _ in range(45):
        prices.append(prices[-1] * 0.98)

    allowed, reason = check_pre_trade_var(
        [{"ticker": "BTCUSDT", "notional_usdt": 1000.0, "direction": "long"}],
        "ETHUSDT",
        1000.0,
        1000.0,
        historical_prices={"BTCUSDT": prices, "ETHUSDT": prices},
        max_var_pct=3.0,
        new_direction="long",
        fail_closed_on_missing_data=True,
    )

    assert allowed is False
    assert "exceeds limit" in reason


def test_portfolio_var_respects_short_direction_offset():
    prices = [100.0]
    for _ in range(45):
        prices.append(prices[-1] * 0.98)

    allowed, reason = check_pre_trade_var(
        [{"ticker": "BTCUSDT", "notional_usdt": 1000.0, "direction": "long"}],
        "BTCUSDT",
        1000.0,
        1000.0,
        historical_prices={"BTCUSDT": prices},
        max_var_pct=1.0,
        new_direction="short",
        fail_closed_on_missing_data=True,
    )

    assert allowed is True
    assert reason == ""


def test_portfolio_var_missing_history_fails_closed_for_live_mode():
    allowed, reason = check_pre_trade_var(
        [{"ticker": "BTCUSDT", "notional_usdt": 500.0, "direction": "long"}],
        "ETHUSDT",
        500.0,
        1000.0,
        historical_prices={"BTCUSDT": [100.0] * 40},
        max_var_pct=5.0,
        new_direction="long",
        fail_closed_on_missing_data=True,
    )

    assert allowed is False
    assert "incomplete" in reason


@pytest.mark.asyncio
async def test_nonce_cache_is_advisory_for_durable_redelivery(monkeypatch):
    monkeypatch.setattr(webhook_module, "_redis_nonce_available", False)
    nonce = f"hardening-{time.time_ns()}"
    timestamp = time.time()

    first_seen = await webhook_module._check_replay_protection(
        nonce,
        timestamp,
        "test-scope",
    )
    second_seen = await webhook_module._check_replay_protection(
        nonce,
        timestamp,
        "test-scope",
    )

    assert first_seen is False
    assert second_seen is True


def _signal(direction: SignalDirection = SignalDirection.LONG, exchange: str = "BINANCE"):
    return TradingViewSignal(
        secret="",
        ticker="BTCUSDT",
        exchange=exchange,
        direction=direction,
        price=50_000.0,
        timeframe="1h",
        strategy="hardening-test",
        message="test",
    )


def test_durable_queue_payload_freezes_omitted_signal_timestamp():
    body = {
        "secret": "test",
        "ticker": "BTCUSDT",
        "direction": "long",
        "price": 50_000.0,
    }
    signal = TradingViewSignal(**body)

    persisted = webhook_module._durable_queue_payload(body, signal)
    reconstructed = TradingViewSignal(**persisted)

    assert "timestamp" not in body
    assert reconstructed.timestamp == signal.timestamp


async def _reserved_event(db_session, signal: TradingViewSignal, *, status: str = "received"):
    payload = signal.model_dump(mode="json")
    fingerprint = compute_webhook_fingerprint(payload)
    event = WebhookEventModel(
        fingerprint=fingerprint,
        ticker=signal.ticker,
        direction=signal.direction.value,
        status=status,
        status_code=202,
        reason="reserved",
        client_ip="test",
        payload_json=__import__("json").dumps(payload),
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    db_session.add(event)
    await db_session.commit()
    return event, fingerprint


@pytest.mark.asyncio
async def test_durable_worker_retries_explicit_pre_execution_error_then_completes(db_session, monkeypatch):
    event, _ = await _reserved_event(db_session, _signal())

    monkeypatch.setattr(
        SignalProcessor,
        "process_webhook",
        AsyncMock(
            return_value={
                "status": "error",
                "reason": "distributed lock timeout",
                "retry_safe": True,
                "failure_stage": "pre_execution",
            }
        ),
    )
    first = await process_webhook_event(event.id)
    await db_session.refresh(event)

    assert first["status"] == "error"
    assert event.status == "retrying"
    assert event.attempt_count == 1
    assert event.next_attempt_at is not None

    event.next_attempt_at = utcnow() - timedelta(seconds=1)
    await db_session.commit()
    monkeypatch.setattr(
        SignalProcessor,
        "process_webhook",
        AsyncMock(return_value={"status": "simulated", "reason": "paper close"}),
    )
    second = await process_webhook_event(event.id)
    await db_session.refresh(event)

    assert second["status"] == "simulated"
    assert event.status == "simulated"
    assert event.attempt_count == 2
    assert event.next_attempt_at is None


@pytest.mark.asyncio
async def test_durable_worker_never_retries_ambiguous_exchange_error(db_session, monkeypatch):
    event, _ = await _reserved_event(db_session, _signal())
    process = AsyncMock(
        return_value={
            "status": "error",
            "reason": "network timeout after create_order",
            "order_id": "exchange-123",
            "client_order_id": "qp_deterministic",
            "requires_reconciliation": True,
        }
    )
    monkeypatch.setattr(SignalProcessor, "process_webhook", process)

    first = await process_webhook_event(event.id)
    await db_session.refresh(event)
    second = await process_webhook_event(event.id)

    assert first["status"] == "error"
    assert second["status"] == "idle"
    assert event.status == "manual_review"
    assert event.next_attempt_at is None
    assert process.await_count == 1
    assert await has_recent_webhook_event(db_session, event.fingerprint, window_secs=1800)


@pytest.mark.asyncio
async def test_durable_worker_claim_is_atomic_on_sqlite(db_session, monkeypatch):
    event, _ = await _reserved_event(db_session, _signal())
    calls = 0

    async def _slow_success(*args, **kwargs):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        return {"status": "accepted", "reason": "processed once"}

    monkeypatch.setattr(SignalProcessor, "process_webhook", _slow_success)
    results = await asyncio.gather(
        process_webhook_event(event.id),
        process_webhook_event(event.id),
    )

    assert calls == 1
    assert sorted(result["status"] for result in results) == ["accepted", "idle"]


@pytest.mark.asyncio
async def test_expired_lease_stops_after_maximum_attempts(db_session):
    event, _ = await _reserved_event(db_session, _signal(), status="processing")
    event.attempt_count = 3
    event.updated_at = utcnow() - timedelta(minutes=10)
    await db_session.commit()

    result = await process_webhook_event(event.id)
    await db_session.refresh(event)

    assert result["status"] == "idle"
    assert event.status == "failed"
    assert event.next_attempt_at is None


@pytest.mark.asyncio
async def test_error_webhook_does_not_block_legitimate_redelivery(db_session):
    event, fingerprint = await _reserved_event(db_session, _signal(), status="error")
    event.status_code = 500
    await db_session.commit()

    assert await has_recent_webhook_event(db_session, fingerprint, window_secs=1800) is None


@pytest.mark.asyncio
async def test_close_signal_bypasses_entry_prefilter_and_paid_ai(db_session, monkeypatch):
    signal = _signal(SignalDirection.CLOSE_LONG)
    event, fingerprint = await _reserved_event(db_session, signal)
    processor = SignalProcessor(db_session)
    monkeypatch.setattr(
        processor,
        "_run_prefilter",
        AsyncMock(side_effect=AssertionError("close must not run entry prefilter")),
    )
    monkeypatch.setattr(
        processor,
        "_run_ai_analysis",
        AsyncMock(side_effect=AssertionError("close must not call paid AI")),
    )
    execute = AsyncMock(return_value={"status": "simulated", "reason": "closed"})
    monkeypatch.setattr(processor, "_execute_trade", execute)

    result = await processor._process_signal_locked(
        signal,
        raw_body=signal.model_dump(mode="json"),
        reserved_event_id=event.id,
        reserved_fingerprint=fingerprint,
        loaded_user_settings={},
    )

    assert result["status"] == "simulated"
    assert result["paid_ai_used"] is False
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_live_tradingview_exchange_mismatch_fails_closed(db_session, monkeypatch):
    signal = _signal(SignalDirection.LONG, exchange="BINANCE")
    event, fingerprint = await _reserved_event(db_session, signal)
    processor = SignalProcessor(db_session)
    monkeypatch.setattr(
        processor,
        "_run_prefilter",
        AsyncMock(side_effect=AssertionError("mismatch must block before analysis")),
    )

    result = await processor._process_signal_locked(
        signal,
        raw_body=signal.model_dump(mode="json"),
        prefetched_market=MarketContext(ticker="BTCUSDT", current_price=50_000.0),
        reserved_event_id=event.id,
        reserved_fingerprint=fingerprint,
        loaded_user_settings={
            "exchange": {
                "name": "bybit",
                "market_type": "contract",
                "live_trading": True,
            }
        },
    )

    assert result["status"] == "blocked"
    assert "does not match" in result["reason"]


@pytest.mark.asyncio
async def test_scanner_observe_mode_does_not_call_paid_ai(db_session, monkeypatch):
    monkeypatch.setattr(settings.scanner, "observe_ai_enabled", False)
    signal = _signal()
    processor = SignalProcessor(db_session)
    monkeypatch.setattr(
        processor,
        "_run_prefilter",
        AsyncMock(return_value=PreFilterResult(passed=True, score=80.0, reason="ok", checks={})),
    )
    monkeypatch.setattr(
        processor,
        "_run_ai_analysis",
        AsyncMock(side_effect=AssertionError("observe mode must be free by default")),
    )

    result = await processor.process_scanner_signal(
        signal,
        scanner_mode="observe",
        scanner_payload={"source": "auto_scanner", "setup_hash": "observe-free"},
        market=MarketContext(ticker="BTCUSDT", current_price=50_000.0),
    )

    assert result["status"] == "observed"
    assert result["paid_ai_used"] is False
    assert result["would_execute"] is None


@pytest.mark.asyncio
async def test_scanner_live_requires_validated_paper_outcomes(db_session, monkeypatch):
    monkeypatch.setattr(settings.exchange, "live_trading", True)
    monkeypatch.setattr(settings.scanner, "live_min_outcome_samples", 30)
    processor = SignalProcessor(db_session)

    result = await processor.process_scanner_signal(
        _signal(),
        scanner_mode="live",
        scanner_payload={"source": "auto_scanner", "setup_hash": "no-paper-history"},
        market=MarketContext(ticker="BTCUSDT", current_price=50_000.0),
    )

    assert result["status"] == "blocked"
    assert "validated paper outcomes" in result["reason"]


def test_high_price_cache_uses_real_relative_bucket():
    base = float(_price_to_bucket(50_000.0))
    nearby = float(_price_to_bucket(50_100.0))
    far = float(_price_to_bucket(51_000.0))

    assert base == nearby
    assert far != base


def test_walk_forward_rejects_negative_validation(monkeypatch):
    monkeypatch.setattr(settings.scanner, "walk_forward_min_samples", 30)
    samples = []
    now = utcnow()
    for idx in range(30):
        samples.append({
            "score": 80.0,
            "pnl_pct": 1.0 if idx < 21 else -1.0,
            "closed_at": (now + timedelta(minutes=idx)).isoformat(),
        })

    assert _best_threshold(samples) is None


def test_provider_request_budget_counts_real_attempts_and_persists(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.ai, "max_provider_requests_per_day", 0)
    monkeypatch.setattr(settings.scanner, "max_ai_calls_per_day", 1)
    journal = tmp_path / "ai_usage.jsonl"
    tracker = AICostTracker()
    tracker.reset()
    tracker._journal_path = journal
    tokens = set_ai_usage_context("auto_scanner")
    try:
        tracker.record_attempt("deepseek", "deepseek-chat")
        with pytest.raises(AIBudgetExceeded):
            tracker.record_attempt("deepseek", "deepseek-chat")
    finally:
        reset_ai_usage_context(tokens)

    restored = AICostTracker()
    restored.reset()
    restored._journal_path = journal
    restored._load_journal()
    assert restored.requests_today("auto_scanner") == 1


def test_global_provider_budget_combines_all_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.ai, "max_provider_requests_per_day", 2)
    monkeypatch.setattr(settings.ai, "webhook_max_provider_requests_per_day", 10)
    monkeypatch.setattr(settings.scanner, "max_ai_calls_per_day", 10)
    tracker = AICostTracker()
    tracker.reset()
    tracker._journal_path = tmp_path / "global_ai_usage.jsonl"

    tradingview_tokens = set_ai_usage_context("tradingview")
    try:
        tracker.record_attempt("deepseek", "deepseek-chat")
    finally:
        reset_ai_usage_context(tradingview_tokens)

    scanner_tokens = set_ai_usage_context("auto_scanner")
    try:
        tracker.record_attempt("deepseek", "deepseek-chat")
        with pytest.raises(AIBudgetExceeded, match="Global daily"):
            tracker.record_attempt("deepseek", "deepseek-chat")
    finally:
        reset_ai_usage_context(scanner_tokens)

    assert tracker.get_summary()["requests_today_global"] == 2


@pytest.mark.asyncio
async def test_admin_position_checks_ignore_other_users(db_session, monkeypatch):
    monkeypatch.setattr(settings.exchange, "live_trading", False)
    monkeypatch.setattr(settings.exchange, "name", "binance")
    db_session.add(
        PositionModel(
            user_id="other-user",
            ticker="BTCUSDT",
            direction="short",
            status="open",
            entry_price=50_000.0,
            quantity=0.01,
            remaining_quantity=0.01,
            opened_at=utcnow(),
        )
    )
    db_session.add(
        PositionModel(
            user_id=None,
            ticker="BTCUSDT",
            direction="short",
            status="open",
            entry_price=50_000.0,
            quantity=0.01,
            remaining_quantity=0.01,
            opened_at=utcnow(),
            exchange="okx",
            live_trading=False,
        )
    )
    await db_session.commit()
    decision = TradeDecision(
        execute=True,
        ticker="BTCUSDT",
        direction=SignalDirection.LONG,
        entry_price=50_000.0,
        quantity=0.01,
    )
    processor = SignalProcessor(db_session)

    conflict, position = await processor._check_position_conflict(decision, None, {})
    correlation = await processor._check_correlation_risk(decision, None, {})

    assert conflict is None
    assert position is None
    assert correlation["current_exposure"]["short_positions"] == 0


@pytest.mark.asyncio
async def test_reverse_close_db_failure_is_preserved_for_manual_reconciliation(db_session, monkeypatch):
    position = PositionModel(
        user_id=None,
        ticker="BTCUSDT",
        direction="long",
        status="open",
        entry_price=50_000.0,
        quantity=0.01,
        remaining_quantity=0.01,
        opened_at=utcnow(),
        live_trading=True,
        exchange="binance",
    )
    db_session.add(position)
    await db_session.commit()

    monkeypatch.setattr(
        "exchange.get_ticker",
        AsyncMock(return_value={"last": 49_900.0}),
    )
    monkeypatch.setattr(
        "services.signal_processor.execute_trade",
        AsyncMock(return_value={"status": "closed", "order_id": "close-1", "exit_price": 49_900.0}),
    )
    monkeypatch.setattr(
        "services.signal_processor.close_position_async",
        AsyncMock(side_effect=RuntimeError("database unavailable")),
    )

    result = await SignalProcessor(db_session)._close_conflicting_position(
        position,
        None,
        {},
    )

    assert result["status"] == "manual_review"
    assert result["requires_reconciliation"] is True
    assert result["exchange_close"]["order_id"] == "close-1"
    assert "database unavailable" in result["database_error"]


@pytest.mark.asyncio
async def test_ambiguous_reverse_close_propagates_reconciliation_fields(db_session, monkeypatch):
    position = PositionModel(
        user_id=None,
        ticker="BTCUSDT",
        direction="long",
        status="open",
        entry_price=50_000.0,
        quantity=0.01,
        remaining_quantity=0.01,
        opened_at=utcnow(),
        live_trading=True,
        exchange="binance",
    )
    db_session.add(position)
    await db_session.commit()
    monkeypatch.setattr("exchange.get_ticker", AsyncMock(return_value={"last": 49_900.0}))
    monkeypatch.setattr(
        "services.signal_processor.execute_trade",
        AsyncMock(
            return_value={
                "status": "manual_review",
                "reason": "close request timed out after submission",
                "order_id": "close-ambiguous-1",
                "requires_reconciliation": True,
            }
        ),
    )

    result = await SignalProcessor(db_session)._close_conflicting_position(
        position,
        None,
        {},
    )

    assert result["status"] == "manual_review"
    assert result["requires_reconciliation"] is True
    assert result["order_id"] == "close-ambiguous-1"


@pytest.mark.asyncio
async def test_order_event_uses_deterministic_id_and_requires_review(db_session):
    decision = TradeDecision(
        execute=True,
        ticker="BTCUSDT",
        direction=SignalDirection.LONG,
        idempotency_key="webhook-fingerprint-123",
    )
    event = await record_order_event(
        db_session,
        decision,
        {
            "status": "error",
            "reason": "network timeout after submission",
            "requires_reconciliation": True,
        },
    )

    expected = hashlib.sha256(decision.idempotency_key.encode("utf-8")).hexdigest()[:24]
    assert event.client_order_id == f"qp_{expected}"
    assert event.status == "manual_review"
    assert event.retry_state == "manual_review"
    assert event.next_retry_at is None


@pytest.mark.asyncio
async def test_scanner_audit_failure_does_not_poison_transaction(db_session):
    audit = await record_scanner_audit(
        db_session,
        event_type="invalid-test",
        score=object(),
    )

    assert audit is None
    result = await db_session.execute(select(PositionModel.id))
    assert result.all() == []


@pytest.mark.asyncio
async def test_scanner_fallback_pins_all_timeframes_and_context_to_one_exchange(monkeypatch):
    calls = []
    now = utcnow().replace(tzinfo=None)

    async def fake_history(ticker, timeframe, **kwargs):
        source = kwargs.get("exchange_ids", [""])[0]
        calls.append(("ohlcv", source, timeframe))
        if source == "binance" and timeframe == "4h":
            return [
                NormalizedCandle(
                    timestamp=now,
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.0,
                    volume=1000.0,
                ).model_dump()
            ]
        return [
            NormalizedCandle(
                timestamp=now - timedelta(hours=80 - idx),
                open=100 + idx,
                high=101 + idx,
                low=99 + idx,
                close=100 + idx,
                volume=1000 + idx,
            ).model_dump()
            for idx in range(80)
        ]

    async def fake_context(ticker, **kwargs):
        source = kwargs.get("exchange_ids", [""])[0]
        calls.append(("context", source, ""))
        context = MarketContext(
            ticker=ticker,
            current_price=179.0,
            volume_24h=50_000_000.0,
            bid_ask_spread=0.02,
        )
        object.__setattr__(context, "_market_data_source", source)
        object.__setattr__(context, "_orderbook_bid_depth_usdt", 250_000.0)
        object.__setattr__(context, "_orderbook_ask_depth_usdt", 250_000.0)
        return context

    monkeypatch.setattr(settings.scanner, "bundle_cache_ttl_secs", 0)
    monkeypatch.setattr(settings.scanner, "data_source_policy", "fallback")
    monkeypatch.setattr(
        "services.unified_ohlcv._market_data_exchange_ids",
        lambda primary, include_fallbacks=True: ["binance", "okx"],
    )
    monkeypatch.setattr("services.unified_ohlcv.fetch_ohlcv_history", fake_history)
    monkeypatch.setattr("services.unified_ohlcv.fetch_market_context", fake_context)

    bundle = await UnifiedOHLCVProvider().get_bundle("BTCUSDT", ["1h", "4h"])

    assert bundle.mapping.actual_data_source == "okx"
    assert bundle.quality_passed is True
    assert ("context", "okx", "") in calls
    assert ("context", "binance", "") not in calls
