from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import exchange as exchange_module
from core.config import settings
from core.exceptions import OrderValidationError
from models import (
    AIAnalysis,
    MarketContext,
    SignalDirection,
    TakeProfitLevel,
    TradeDecision,
    TradingViewSignal,
    TrailingStopConfig,
    TrailingStopMode,
)
from services.signal_processor import SignalProcessor


def _set_global_exchange_defaults(monkeypatch):
    monkeypatch.setattr(settings.exchange, "name", "binance")
    monkeypatch.setattr(settings.exchange, "api_key", "GLOBAL_KEY")
    monkeypatch.setattr(settings.exchange, "api_secret", "GLOBAL_SECRET")
    monkeypatch.setattr(settings.exchange, "password", "GLOBAL_PASSWORD")
    monkeypatch.setattr(settings.exchange, "live_trading", True)
    monkeypatch.setattr(settings.exchange, "sandbox_mode", False)
    monkeypatch.setattr(settings.exchange, "market_type", "contract")


def _user_exchange_config() -> dict:
    return {
        "exchange": "okx",
        "api_key": "",
        "api_secret": "",
        "password": "",
        "live_trading": True,
        "sandbox_mode": True,
        "market_type": "contract",
    }


def _capture_exchange_kwargs(monkeypatch, fake_exchange):
    captured = {}

    def fake_get_or_create_exchange(**kwargs):
        # Only capture calls from actual trade execution (live=True);
        # ignore utility calls like get_market_limits which pass live=False.
        if kwargs.get("live"):
            captured.update(kwargs)
        return fake_exchange

    monkeypatch.setattr(exchange_module, "_get_or_create_exchange", fake_get_or_create_exchange)
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "orderbook",
    [
        {},
        {"asks": [[100.0, 0.1]], "bids": [[99.9, 10.0]]},
        {"asks": [[101.0, 10.0]], "bids": [[100.9, 10.0]]},
    ],
)
async def test_market_order_slippage_protection_fails_closed(
    monkeypatch,
    orderbook,
):
    created = []
    fake_exchange = SimpleNamespace(
        id="binance",
        fetch_order_book=lambda *args, **kwargs: orderbook,
        create_order=lambda **kwargs: created.append(kwargs),
    )
    monkeypatch.setattr(
        exchange_module,
        "_validate_and_adjust_amount",
        lambda exchange, symbol, amount, allow_amount_increase: amount,
    )

    with pytest.raises(OrderValidationError):
        await exchange_module._create_exchange_order(
            fake_exchange,
            symbol="BTC/USDT:USDT",
            order_type="market",
            side="buy",
            amount=1.0,
            max_slippage_pct=0.5,
            slippage_reference_price=100.0,
        )

    assert created == []


def _assert_empty_credentials(captured: dict):
    assert captured["exchange_id"] == "okx"
    # Both None and "" represent empty credentials
    assert captured["api_key"] in ("", None)
    assert captured["api_secret"] in ("", None)
    assert captured["password"] in ("", None)
    assert captured["sandbox"] is True
    assert captured["market_type"] == "contract"


@pytest.mark.asyncio
async def test_execute_trade_rejects_user_live_without_credentials(monkeypatch):
    processor = SignalProcessor(session=AsyncMock())
    decision = processor._build_trade_decision(
        TradingViewSignal(
            secret="test",
            ticker="BTCUSDT",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=100.0,
            timeframe="60",
            strategy="test",
            message="",
        ),
        AIAnalysis(
            confidence=0.8,
            recommendation="execute",
            reasoning="ok",
            suggested_stop_loss=95.0,
            suggested_tp1=110.0,
            tp1_qty_pct=100.0,
            tp2_qty_pct=0.0,
            tp3_qty_pct=0.0,
            tp4_qty_pct=0.0,
        ),
        MarketContext(ticker="BTCUSDT", current_price=100.0),
        None,
        {},
    )

    monkeypatch.setattr(settings.exchange, "name", "binance")
    monkeypatch.setattr(settings.exchange, "api_key", "GLOBAL_KEY")
    monkeypatch.setattr(settings.exchange, "api_secret", "GLOBAL_SECRET")
    monkeypatch.setattr(settings.exchange, "password", "GLOBAL_PASSWORD")
    monkeypatch.setattr(settings.exchange, "live_trading", True)
    monkeypatch.setattr(settings.exchange, "sandbox_mode", False)
    monkeypatch.setattr(settings.exchange, "market_type", "contract")
    monkeypatch.setattr(settings.exchange, "default_order_type", "market")
    monkeypatch.setattr(settings.exchange, "stop_loss_order_type", "market")
    monkeypatch.setattr(settings.risk, "max_position_pct", 10.0)

    fake_user = SimpleNamespace(live_trading_allowed=True, max_leverage=20, max_position_pct=10.0)

    fake_execute_trade = AsyncMock(return_value={"status": "simulated"})

    monkeypatch.setattr("services.signal_processor.get_user_by_id", AsyncMock(return_value=fake_user))
    monkeypatch.setattr("services.signal_processor.get_user_active_subscription", AsyncMock(return_value=object()))
    monkeypatch.setattr("services.signal_processor.trading_allowed", AsyncMock(return_value={"allowed": True}))
    monkeypatch.setattr("services.signal_processor.execute_trade", fake_execute_trade)
    monkeypatch.setattr(
        "services.signal_processor.log_trade_db",
        AsyncMock(return_value=SimpleNamespace(id="trade-1", payload_json="{}")),
    )
    monkeypatch.setattr("services.signal_processor.record_order_event", AsyncMock(return_value=SimpleNamespace(id="evt-1")))
    monkeypatch.setattr("services.signal_processor.notify_trade_executed", AsyncMock())
    monkeypatch.setattr("services.signal_processor.record_trade", lambda *args, **kwargs: None)

    user_settings = {
        "exchange": {
            "name": "okx",
            "api_key": "",
            "api_secret": "",
            "password": "",
            "live_trading": True,
            "sandbox_mode": True,
            "market_type": "contract",
            "default_order_type": "limit",
            "stop_loss_order_type": "market",
            "limit_timeout_overrides": {"1h": 3600},
        }
    }

    result = await processor._execute_trade(decision, "user-1", user_settings)

    assert result["status"] == "rejected"
    assert "credentials" in result["reason"]
    fake_execute_trade.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_trade_preserves_explicit_empty_limit_timeout_overrides(monkeypatch):
    processor = SignalProcessor(session=AsyncMock())
    decision = processor._build_trade_decision(
        TradingViewSignal(
            secret="test",
            ticker="BTCUSDT",
            exchange="BINANCE",
            direction=SignalDirection.LONG,
            price=100.0,
            timeframe="60",
            strategy="test",
            message="",
        ),
        AIAnalysis(
            confidence=0.8,
            recommendation="execute",
            reasoning="ok",
            suggested_stop_loss=95.0,
            suggested_tp1=110.0,
            tp1_qty_pct=100.0,
            tp2_qty_pct=0.0,
            tp3_qty_pct=0.0,
            tp4_qty_pct=0.0,
        ),
        MarketContext(ticker="BTCUSDT", current_price=100.0),
        None,
        {},
    )

    monkeypatch.setattr(settings.exchange, "name", "binance")
    monkeypatch.setattr(settings.exchange, "api_key", "GLOBAL_KEY")
    monkeypatch.setattr(settings.exchange, "api_secret", "GLOBAL_SECRET")
    monkeypatch.setattr(settings.exchange, "password", "GLOBAL_PASSWORD")
    monkeypatch.setattr(settings.exchange, "live_trading", True)
    monkeypatch.setattr(settings.exchange, "sandbox_mode", False)
    monkeypatch.setattr(settings.exchange, "market_type", "contract")
    monkeypatch.setattr(settings.exchange, "default_order_type", "market")
    monkeypatch.setattr(settings.exchange, "stop_loss_order_type", "market")
    monkeypatch.setattr(settings.exchange, "limit_timeout_overrides", {"1h": 21600})
    monkeypatch.setattr(settings.risk, "max_position_pct", 10.0)

    fake_user = SimpleNamespace(live_trading_allowed=True, max_leverage=20, max_position_pct=10.0)

    async def fake_execute_trade(_decision, exchange_config):
        return {"status": "simulated", "captured_exchange_config": dict(exchange_config)}

    monkeypatch.setattr("services.signal_processor.get_user_by_id", AsyncMock(return_value=fake_user))
    monkeypatch.setattr("services.signal_processor.get_user_active_subscription", AsyncMock(return_value=object()))
    monkeypatch.setattr("services.signal_processor.trading_allowed", AsyncMock(return_value={"allowed": True}))
    monkeypatch.setattr("services.signal_processor.execute_trade", fake_execute_trade)
    monkeypatch.setattr(
        "services.signal_processor.log_trade_db",
        AsyncMock(return_value=SimpleNamespace(id="trade-1", payload_json="{}")),
    )
    monkeypatch.setattr("services.signal_processor.record_order_event", AsyncMock(return_value=SimpleNamespace(id="evt-1")))
    monkeypatch.setattr("services.signal_processor.notify_trade_executed", AsyncMock())
    monkeypatch.setattr("services.signal_processor.record_trade", lambda *args, **kwargs: None)

    result = await processor._execute_trade(
        decision,
        "user-1",
        {"exchange": {"name": "okx", "limit_timeout_overrides": {}, "live_trading": "false", "sandbox_mode": "false"}},
    )

    config = result["captured_exchange_config"]
    assert config["limit_timeout_overrides"] == {}
    assert config["live_trading"] is False
    assert config["sandbox_mode"] is False


@pytest.mark.asyncio
async def test_user_live_entry_requires_global_master_switch(monkeypatch):
    processor = SignalProcessor(session=AsyncMock())
    decision = TradeDecision(
        execute=True,
        direction=SignalDirection.LONG,
        ticker="BTCUSDT",
        entry_price=100.0,
        quantity=1.0,
    )
    monkeypatch.setattr(settings.exchange, "live_trading", False)
    monkeypatch.setattr(
        "services.signal_processor.get_user_by_id",
        AsyncMock(return_value=SimpleNamespace(role="user")),
    )

    result = await processor._execute_trade(
        decision,
        "user-1",
        {
            "exchange": {
                "name": "binance",
                "api_key": "user-key",
                "api_secret": "user-secret",
                "live_trading": True,
            }
        },
    )

    assert result["status"] == "rejected"
    assert "global LIVE_TRADING master switch" in result["reason"]


@pytest.mark.asyncio
async def test_execute_trade_applies_exchange_overrides_without_user_id(monkeypatch):
    processor = SignalProcessor(session=AsyncMock())
    decision = TradeDecision(
        execute=True,
        direction=SignalDirection.LONG,
        ticker="BTCUSDT",
        entry_price=100.0,
        quantity=1.0,
        order_type="market",
    )

    monkeypatch.setattr(settings.exchange, "name", "binance")
    monkeypatch.setattr(settings.exchange, "api_key", "GLOBAL_KEY")
    monkeypatch.setattr(settings.exchange, "api_secret", "GLOBAL_SECRET")
    monkeypatch.setattr(settings.exchange, "password", "GLOBAL_PASSWORD")
    monkeypatch.setattr(settings.exchange, "live_trading", True)
    monkeypatch.setattr(settings.exchange, "sandbox_mode", False)
    monkeypatch.setattr(settings.exchange, "market_type", "contract")
    monkeypatch.setattr(settings.exchange, "default_order_type", "market")
    monkeypatch.setattr(settings.exchange, "stop_loss_order_type", "market")
    monkeypatch.setattr(settings.risk, "max_position_pct", 10.0)

    async def fake_execute_trade(_decision, exchange_config):
        return {"status": "simulated", "captured_exchange_config": dict(exchange_config)}

    monkeypatch.setattr(processor, "_apply_position_limits", lambda *args, **kwargs: None)
    monkeypatch.setattr("services.signal_processor.trading_allowed", AsyncMock(return_value={"allowed": True}))
    monkeypatch.setattr("services.signal_processor.execute_trade", fake_execute_trade)
    monkeypatch.setattr(
        "services.signal_processor.log_trade_db",
        AsyncMock(return_value=SimpleNamespace(id="trade-1", payload_json="{}")),
    )
    monkeypatch.setattr("services.signal_processor.record_order_event", AsyncMock(return_value=SimpleNamespace(id="evt-1")))
    monkeypatch.setattr("services.signal_processor.notify_trade_executed", AsyncMock())
    monkeypatch.setattr("services.signal_processor.record_trade", lambda *args, **kwargs: None)

    result = await processor._execute_trade(
        decision,
        None,
        {"exchange": {"name": "okx", "live_trading": False, "market_type": "contract"}},
    )

    config = result["captured_exchange_config"]
    assert config["exchange"] == "okx"
    assert config["live_trading"] is False
    assert config["market_type"] == "contract"


@pytest.mark.asyncio
async def test_exchange_execute_trade_preserves_explicit_empty_credentials(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)

    fake_exchange = SimpleNamespace(options={"defaultType": "future"})
    captured = _capture_exchange_kwargs(monkeypatch, fake_exchange)

    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    monkeypatch.setattr(
        exchange_module,
        "_create_exchange_order",
        AsyncMock(return_value={"id": "entry-1", "status": "closed", "filled": 1.0, "average": 100.0}),
    )

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.LONG,
            ticker="BTCUSDT",
            entry_price=100.0,
            quantity=1.0,
            order_type="market",
        ),
        _user_exchange_config(),
    )

    assert result["status"] == "filled"
    _assert_empty_credentials(captured)


@pytest.mark.asyncio
async def test_exchange_accepted_order_without_id_requires_reconciliation(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)
    monkeypatch.setattr(
        exchange_module,
        "_get_or_create_exchange",
        lambda **kwargs: SimpleNamespace(options={"defaultType": "future"}),
    )
    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    monkeypatch.setattr(
        exchange_module,
        "_create_exchange_order",
        AsyncMock(return_value={"status": "closed", "filled": 1.0, "average": 100.0}),
    )

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.LONG,
            ticker="BTCUSDT",
            entry_price=100.0,
            quantity=1.0,
            order_type="market",
            idempotency_key="accepted-no-id",
        ),
        _user_exchange_config(),
    )

    assert result["status"] == "manual_review"
    assert result["requires_reconciliation"] is True
    assert result["failure_stage"] == "post_submission"
    assert result["client_order_id"] == exchange_module.client_order_id_for_idempotency("accepted-no-id")


@pytest.mark.asyncio
async def test_exchange_execute_trade_treats_string_false_as_paper(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)
    monkeypatch.setattr(exchange_module, "get_market_limits", lambda *args, **kwargs: {})

    def fail_if_live(*args, **kwargs):
        raise AssertionError("live exchange client should not be created")

    monkeypatch.setattr(exchange_module, "_get_or_create_exchange", fail_if_live)

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.LONG,
            ticker="BTCUSDT",
            entry_price=100.0,
            quantity=1.0,
            order_type="market",
        ),
        {"live_trading": "false", "sandbox_mode": "false"},
    )

    assert result["status"] == "simulated"


@pytest.mark.asyncio
async def test_exchange_execute_trade_blocks_live_entry_when_global_live_disabled(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(settings.exchange, "live_trading", False)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.LONG,
            ticker="BTCUSDT",
            entry_price=100.0,
            quantity=1.0,
            order_type="market",
        ),
        {"live_trading": True, "sandbox_mode": False},
    )

    assert result["status"] == "rejected"
    assert "LIVE_TRADING=false" in result["reason"]


@pytest.mark.asyncio
async def test_execute_trade_close_uses_requested_quantity(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)
    monkeypatch.setattr(exchange_module, "_get_or_create_exchange", lambda **kwargs: SimpleNamespace(options={}))
    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    close_position = AsyncMock(return_value={"status": "partial_closed", "remaining_contracts": 0.75})
    monkeypatch.setattr(exchange_module, "_close_position", close_position)

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.CLOSE_LONG,
            ticker="BTCUSDT",
            quantity=0.25,
            idempotency_key="webhook-close-1",
        ),
        {"live_trading": True, "sandbox_mode": False, "market_type": "contract"},
    )

    assert result["status"] == "partial_closed"
    close_position.assert_awaited_once()
    assert close_position.await_args.kwargs["close_quantity"] == pytest.approx(0.25)
    assert close_position.await_args.kwargs["client_order_id"].startswith("qp_")


@pytest.mark.asyncio
async def test_execute_trade_rejects_live_limit_without_atomic_protection(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)

    fake_exchange = SimpleNamespace(options={"defaultType": "future"})
    _capture_exchange_kwargs(monkeypatch, fake_exchange)

    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    monkeypatch.setattr(
        exchange_module,
        "_create_exchange_order",
        AsyncMock(return_value={
            "id": "entry-1",
            "status": "open",
            "filled": 0.0,
            "price": 100.0,
            "_requested_amount": 1.0,
            "_submitted_amount": 2.0,
        }),
    )

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.LONG,
            ticker="BTCUSDT",
            entry_price=100.0,
            quantity=1.0,
            stop_loss=98.0,
            take_profit_levels=[TakeProfitLevel(price=103.0, qty_pct=100.0)],
            order_type="limit",
            limit_timeout_secs=3600,
        ),
        _user_exchange_config(),
    )

    assert result["status"] == "rejected"
    assert result["failure_stage"] == "pre_execution"
    assert "cannot be attached atomically" in result["reason"]
    exchange_module._create_exchange_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_exchange_cancel_order_preserves_explicit_empty_credentials(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)

    fake_exchange = SimpleNamespace(options={"defaultType": "future"})
    captured = _capture_exchange_kwargs(monkeypatch, fake_exchange)

    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    monkeypatch.setattr(
        exchange_module,
        "_cancel_exchange_order",
        AsyncMock(return_value={"status": "cancelled", "order_id": "ord-1", "symbol": "BTC/USDT:USDT"}),
    )

    result = await exchange_module.cancel_order("ord-1", "BTCUSDT", _user_exchange_config())

    assert result["status"] == "cancelled"
    _assert_empty_credentials(captured)


@pytest.mark.asyncio
async def test_exchange_place_protective_stop_preserves_explicit_empty_credentials(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)

    fake_exchange = SimpleNamespace(options={"defaultType": "future"})
    captured = _capture_exchange_kwargs(monkeypatch, fake_exchange)

    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    monkeypatch.setattr(exchange_module, "_create_conditional_order", AsyncMock(return_value={"id": "stop-1"}))

    result = await exchange_module.place_protective_stop(
        ticker="BTCUSDT",
        direction="long",
        quantity=1.0,
        stop_price=95.0,
        exchange_config=_user_exchange_config(),
    )

    assert result["status"] == "placed"
    _assert_empty_credentials(captured)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_name", "expected_key", "expected_value"),
    [
        ("get_account_balance", "total_quote", 1000.0),
        ("get_balance", "total", {"USDT": 1000.0}),
        ("get_ticker", "symbol", "BTC/USDT:USDT"),
        ("get_latest_candle", "close", 100.0),
        ("get_open_orders", "id", "order-1"),
        ("get_open_positions", "contracts", 1.0),
        ("get_recent_orders", "id", "order-1"),
    ],
)
async def test_exchange_query_paths_preserve_explicit_empty_credentials(
    monkeypatch,
    call_name,
    expected_key,
    expected_value,
):
    _set_global_exchange_defaults(monkeypatch)

    class FakeExchange:
        options = {"defaultType": "future"}

        def fetch_balance(self):
            return {
                "total": {"USDT": 1000.0},
                "free": {"USDT": 900.0},
                "used": {"USDT": 100.0},
                "timestamp": 1,
                "datetime": "2024-01-01T00:00:00Z",
            }

        def fetch_ticker(self, symbol):
            return {
                "symbol": symbol,
                "last": 100.0,
                "bid": 99.5,
                "ask": 100.5,
                "high": 110.0,
                "low": 90.0,
                "volume": 123.0,
                "timestamp": 1,
                "datetime": "2024-01-01T00:00:00Z",
                "close": 100.0,
            }

        def fetch_ohlcv(self, symbol, timeframe, since, limit):
            return [[1, 99.0, 101.0, 98.0, 100.0, 123.0]]

        def fetch_positions(self):
            return [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1.0}]

        def fetch_open_orders(self, symbol=None):
            return [
                {
                    "id": "order-1",
                    "symbol": symbol or "BTC/USDT:USDT",
                    "side": "sell",
                    "type": "limit",
                    "price": 110.0,
                    "amount": 1.0,
                    "remaining": 1.0,
                    "status": "open",
                    "timestamp": 1,
                    "datetime": "2024-01-01T00:00:00Z",
                }
            ]

        def fetch_closed_orders(self, symbol=None, since=None, limit=None):
            return [
                {
                    "id": "order-1",
                    "symbol": symbol or "BTC/USDT:USDT",
                    "side": "buy",
                    "type": "market",
                    "price": 100.0,
                    "average": 100.0,
                    "amount": 1.0,
                    "cost": 100.0,
                    "filled": 1.0,
                    "status": "closed",
                    "timestamp": 1,
                    "datetime": "2024-01-01T00:00:00Z",
                }
            ]

    fake_exchange = FakeExchange()
    captured = _capture_exchange_kwargs(monkeypatch, fake_exchange)

    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    exchange_config = _user_exchange_config()

    if call_name == "get_account_balance":
        result = await exchange_module.get_account_balance(exchange_config)
    elif call_name == "get_balance":
        result = await exchange_module.get_balance(exchange_config)
    elif call_name == "get_ticker":
        result = await exchange_module.get_ticker("BTCUSDT", exchange_config)
    elif call_name == "get_latest_candle":
        result = await exchange_module.get_latest_candle("BTCUSDT", "1h", exchange_config)
    elif call_name == "get_open_orders":
        result = await exchange_module.get_open_orders("BTCUSDT", exchange_config)
        result = result[0]
    elif call_name == "get_open_positions":
        result = await exchange_module.get_open_positions(exchange_config)
        result = result[0]
    else:
        result = await exchange_module.get_recent_orders("BTCUSDT", 10, exchange_config)
        result = result[0]

    assert result[expected_key] == expected_value
    _assert_empty_credentials(captured)


@pytest.mark.asyncio
async def test_get_open_orders_includes_okx_algo_orders(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)

    class FakeExchange:
        id = "okx"
        options = {"defaultType": "future"}

        def __init__(self):
            self.algo_params = None
            self.markets = {
                "BTC/USDT:USDT": {
                    "id": "BTC-USDT-SWAP",
                    "contract": True,
                    "swap": True,
                }
            }

        def load_markets(self):
            return self.markets

        def market(self, symbol):
            return self.markets[symbol]

        def fetch_open_orders(self, symbol=None):
            return [
                {
                    "id": "limit-1",
                    "symbol": symbol,
                    "side": "sell",
                    "type": "limit",
                    "amount": 1.0,
                    "remaining": 1.0,
                    "status": "open",
                }
            ]

        def privateGetTradeOrdersAlgoPending(self, params=None):
            self.algo_params = dict(params or {})
            return {
                "data": [
                    {
                        "algoId": "algo-sl-1",
                        "instId": "BTC-USDT-SWAP",
                        "side": "sell",
                        "ordType": "conditional",
                        "sz": "1",
                        "state": "live",
                        "slTriggerPx": "95",
                        "cTime": "1710000000000",
                    }
                ]
            }

    fake_exchange = FakeExchange()
    monkeypatch.setattr(exchange_module, "_get_or_create_exchange", lambda **kwargs: fake_exchange)

    result = await exchange_module.get_open_orders("BTCUSDT", _user_exchange_config())

    assert {order["id"] for order in result} == {"limit-1", "algo-sl-1"}
    assert fake_exchange.algo_params == {"instId": "BTC-USDT-SWAP"}
    algo_order = next(order for order in result if order["id"] == "algo-sl-1")
    assert algo_order["source"] == "okx_algo"
    assert algo_order["remaining"] == "1"


@pytest.mark.asyncio
async def test_execute_trade_rolls_back_partial_fill_when_protection_fails(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)
    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    monkeypatch.setattr(exchange_module, "_get_or_create_exchange", lambda **kwargs: SimpleNamespace(options={"defaultType": "future"}))
    monkeypatch.setattr(
        exchange_module,
        "_create_exchange_order",
        AsyncMock(return_value={"id": "entry-1", "status": "open", "filled": 0.5, "average": 100.0}),
    )
    monkeypatch.setattr(exchange_module, "_create_conditional_order", AsyncMock(side_effect=RuntimeError("protect fail")))
    monkeypatch.setattr(
        exchange_module,
        "_cancel_exchange_order",
        AsyncMock(return_value={"status": "cancelled", "order_id": "entry-1"}),
    )
    close_position = AsyncMock(return_value={"status": "closed", "order_id": "close-1", "exit_price": 99.0})
    monkeypatch.setattr(exchange_module, "_close_position", close_position)

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.LONG,
            ticker="BTCUSDT",
            entry_price=100.0,
            quantity=1.0,
            take_profit=110.0,
            stop_loss=95.0,
            order_type="market",
        ),
        _user_exchange_config(),
    )

    assert result["status"] == "error"
    assert result["rollback_success"] is True
    assert close_position.await_args.kwargs["close_quantity"] == 0.5


@pytest.mark.asyncio
async def test_partial_fill_cancel_failure_protects_maximum_exposure(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)
    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    fake_exchange = SimpleNamespace(
        options={"defaultType": "future"},
        fetch_order=lambda order_id, symbol: {
            "id": order_id,
            "status": "open",
            "filled": 0.5,
            "remaining": 0.5,
            "average": 100.0,
        },
    )
    monkeypatch.setattr(exchange_module, "_get_or_create_exchange", lambda **kwargs: fake_exchange)
    monkeypatch.setattr(
        exchange_module,
        "_create_exchange_order",
        AsyncMock(return_value={"id": "entry-1", "status": "open", "filled": 0.5, "average": 100.0}),
    )
    monkeypatch.setattr(
        exchange_module,
        "_cancel_exchange_order",
        AsyncMock(return_value={"status": "error", "order_id": "entry-1"}),
    )
    create_protection = AsyncMock(
        side_effect=[
            {"id": "tp-1"},
            {"id": "sl-1"},
        ]
    )
    monkeypatch.setattr(exchange_module, "_create_conditional_order", create_protection)

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.LONG,
            ticker="BTCUSDT",
            entry_price=100.0,
            quantity=1.0,
            take_profit=110.0,
            stop_loss=95.0,
            order_type="market",
        ),
        _user_exchange_config(),
    )

    assert result["status"] == "partial"
    assert result["requires_reconciliation"] is True
    assert result["entry_remainder_cancel_confirmed"] is False
    assert result["take_profit_protected_qty"] == pytest.approx(1.0)
    assert result["stop_loss_protected_qty"] == pytest.approx(1.0)
    assert create_protection.await_args_list[0].args[4] == pytest.approx(1.0)
    assert create_protection.await_args_list[1].args[4] == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_missing_stop_order_id_is_protection_failure_and_rolls_back(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)
    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    monkeypatch.setattr(
        exchange_module,
        "_get_or_create_exchange",
        lambda **kwargs: SimpleNamespace(options={"defaultType": "future"}),
    )
    monkeypatch.setattr(
        exchange_module,
        "_create_exchange_order",
        AsyncMock(return_value={"id": "entry-1", "status": "closed", "filled": 1.0, "average": 100.0}),
    )
    monkeypatch.setattr(exchange_module, "_create_conditional_order", AsyncMock(return_value={}))
    close_position = AsyncMock(
        return_value={"status": "closed", "order_id": "close-1", "exit_price": 99.0}
    )
    monkeypatch.setattr(exchange_module, "_close_position", close_position)

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.LONG,
            ticker="BTCUSDT",
            entry_price=100.0,
            quantity=1.0,
            stop_loss=95.0,
            order_type="market",
        ),
        _user_exchange_config(),
    )

    assert result["status"] == "error"
    assert result["rollback_success"] is True
    assert "SL:" in result["protection_errors"][0]
    close_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_trade_rolls_back_when_multi_tp_reports_failed(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)
    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    monkeypatch.setattr(exchange_module, "_get_or_create_exchange", lambda **kwargs: SimpleNamespace(options={"defaultType": "future"}))
    monkeypatch.setattr(
        exchange_module,
        "_create_exchange_order",
        AsyncMock(return_value={"id": "entry-1", "status": "closed", "filled": 1.0, "average": 100.0}),
    )
    monkeypatch.setattr(
        exchange_module,
        "_place_multi_tp_orders",
        AsyncMock(return_value=[{"level": 1, "status": "failed", "error": "rejected"}]),
    )
    close_position = AsyncMock(return_value={"status": "closed", "order_id": "close-1", "exit_price": 99.0})
    monkeypatch.setattr(exchange_module, "_close_position", close_position)

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.LONG,
            ticker="BTCUSDT",
            entry_price=100.0,
            quantity=1.0,
            take_profit_levels=[TakeProfitLevel(price=110.0, qty_pct=100.0)],
            order_type="market",
        ),
        _user_exchange_config(),
    )

    assert result["status"] == "error"
    assert result["rollback_success"] is True
    assert "Multi-TP failed" in result["protection_errors"][0]


@pytest.mark.asyncio
async def test_conditional_order_network_timeout_does_not_try_another_format(monkeypatch):
    create_order = AsyncMock(side_effect=exchange_module.ccxt.NetworkError("timeout"))
    monkeypatch.setattr(exchange_module, "_create_exchange_order", create_order)
    journal = Mock(return_value="recon-test")
    monkeypatch.setattr("core.reconciliation_journal.record_reconciliation_issue", journal)
    fake_exchange = SimpleNamespace(id="binance", options={"defaultMarginMode": "cross"})

    with pytest.raises(exchange_module.ccxt.NetworkError):
        await exchange_module._create_conditional_order(
            fake_exchange,
            "BTC/USDT:USDT",
            "stop_loss",
            "sell",
            1.0,
            95.0,
            "long",
            client_order_id="qp-stop-test",
        )

    create_order.assert_awaited_once()
    journal.assert_called_once()


@pytest.mark.asyncio
async def test_moving_trailing_stop_is_always_reduce_only(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)
    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    monkeypatch.setattr(
        exchange_module,
        "_get_or_create_exchange",
        lambda **kwargs: SimpleNamespace(options={"defaultType": "future"}),
    )
    create_order = AsyncMock(
        side_effect=[
            {"id": "entry-1", "status": "closed", "filled": 1.0, "average": 100.0},
            {"id": "trail-1", "status": "open"},
        ]
    )
    monkeypatch.setattr(exchange_module, "_create_exchange_order", create_order)

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.LONG,
            ticker="BTCUSDT",
            entry_price=100.0,
            quantity=1.0,
            order_type="market",
            trailing_stop=TrailingStopConfig(mode=TrailingStopMode.MOVING, trail_pct=1.0),
        ),
        _user_exchange_config(),
    )

    assert result["status"] == "filled"
    trailing_call = create_order.await_args_list[1]
    assert trailing_call.kwargs["reduce_only"] is True
    assert trailing_call.kwargs["params"]["reduceOnly"] is True
    assert trailing_call.kwargs["client_order_id"].startswith("qp_ts_")


@pytest.mark.asyncio
async def test_ambiguous_stop_submission_closes_exposure_and_requires_manual_review(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)
    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    monkeypatch.setattr(
        exchange_module,
        "_get_or_create_exchange",
        lambda **kwargs: SimpleNamespace(options={"defaultType": "future"}),
    )
    monkeypatch.setattr(
        exchange_module,
        "_create_exchange_order",
        AsyncMock(return_value={"id": "entry-1", "status": "closed", "filled": 1.0, "average": 100.0}),
    )
    monkeypatch.setattr(
        exchange_module,
        "_create_conditional_order",
        AsyncMock(side_effect=exchange_module.ccxt.NetworkError("timeout")),
    )
    close_position = AsyncMock(return_value={"status": "closed", "order_id": "close-1", "exit_price": 99.0})
    monkeypatch.setattr(exchange_module, "_close_position", close_position)

    result = await exchange_module.execute_trade(
        TradeDecision(
            execute=True,
            direction=SignalDirection.LONG,
            ticker="BTCUSDT",
            entry_price=100.0,
            quantity=1.0,
            stop_loss=95.0,
            order_type="market",
        ),
        _user_exchange_config(),
    )

    assert result["status"] == "manual_review"
    assert result["current_exposure_closed"] is True
    assert result["requires_reconciliation"] is True
    assert result["rollback_success"] is False
    close_position.assert_awaited_once()


@pytest.mark.asyncio
async def test_close_position_requires_exchange_flat_confirmation(monkeypatch):
    monkeypatch.setattr(exchange_module.asyncio, "sleep", AsyncMock())

    class FakeExchange:
        id = "binance"
        options = {"defaultType": "future"}

        def __init__(self):
            self.fetch_calls = 0

        def load_markets(self):
            return {"BTC/USDT:USDT": {"limits": {"amount": {}}, "precision": {"amount": 8}}}

        def fetch_positions(self, symbols):
            self.fetch_calls += 1
            if self.fetch_calls == 1:
                return [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1.0, "markPrice": 100.0}]
            return [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 0.25, "markPrice": 99.0}]

        def create_order(self, **kwargs):
            return {"id": "close-1", "status": "closed", "filled": kwargs["amount"], "average": 99.0}

    result = await exchange_module._close_position(FakeExchange(), "BTC/USDT:USDT", position_side="long")

    assert result["status"] == "close_unconfirmed"
    assert result["remaining_contracts"] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_close_position_uses_new_id_for_terminal_partial_residual(monkeypatch):
    monkeypatch.setattr(exchange_module.asyncio, "sleep", AsyncMock())

    class FakeExchange:
        id = "binance"
        options = {"defaultType": "future"}

        def __init__(self):
            self.remaining = 1.0
            self.orders = []

        def load_markets(self):
            return {"BTC/USDT:USDT": {"limits": {"amount": {}}, "precision": {"amount": 8}}}

        def fetch_positions(self, symbols):
            if self.remaining <= 0:
                return []
            return [{
                "symbol": "BTC/USDT:USDT",
                "side": "long",
                "contracts": self.remaining,
                "markPrice": 100.0,
            }]

        def fetch_orders(self, symbol, since=None, limit=None):
            return list(self.orders)

        def create_order(self, **kwargs):
            fill = 0.5 if not self.orders else self.remaining
            self.remaining = max(0.0, self.remaining - fill)
            order = {
                "id": f"close-{len(self.orders) + 1}",
                "clientOrderId": kwargs["params"]["clientOrderId"],
                "status": "closed",
                "filled": fill,
                "average": 99.0,
            }
            self.orders.append(order)
            return order

    fake_exchange = FakeExchange()
    result = await exchange_module._close_position(
        fake_exchange,
        "BTC/USDT:USDT",
        position_side="long",
        client_order_id="qp_close_base",
    )

    assert result["status"] == "closed"
    assert [order["clientOrderId"] for order in fake_exchange.orders] == [
        "qp_close_base",
        "qp_close_base_r2",
    ]
    assert result["close_verification"]["consecutive_flat_reads"] == 2


@pytest.mark.asyncio
async def test_close_position_partial_quantity_does_not_full_close(monkeypatch):
    monkeypatch.setattr(exchange_module.asyncio, "sleep", AsyncMock())

    class FakeExchange:
        id = "binance"
        options = {"defaultType": "future"}

        def __init__(self):
            self.remaining = 2.0
            self.create_calls = []

        def load_markets(self):
            return {"BTC/USDT:USDT": {"limits": {"amount": {}}, "precision": {"amount": 8}}}

        def fetch_positions(self, symbols):
            if self.remaining <= 0:
                return []
            return [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": self.remaining, "markPrice": 100.0}]

        def create_order(self, **kwargs):
            self.create_calls.append(kwargs)
            self.remaining = max(0.0, self.remaining - kwargs["amount"])
            return {"id": "close-partial", "status": "closed", "filled": kwargs["amount"], "average": 99.0}

    fake_exchange = FakeExchange()

    result = await exchange_module._close_position(
        fake_exchange,
        "BTC/USDT:USDT",
        position_side="long",
        close_quantity=0.5,
        client_order_id="qp_partial",
    )

    assert result["status"] == "partial_closed"
    assert result["remaining_contracts"] == pytest.approx(1.5)
    assert fake_exchange.create_calls[0]["amount"] == pytest.approx(0.5)
    assert fake_exchange.create_calls[0]["params"]["clientOrderId"] == "qp_partial"


@pytest.mark.asyncio
async def test_close_position_retries_reduce_only_until_flat(monkeypatch):
    monkeypatch.setattr(exchange_module.asyncio, "sleep", AsyncMock())

    class FakeExchange:
        id = "binance"
        options = {"defaultType": "future"}

        def __init__(self):
            self.remaining = 1.0
            self.close_amounts = []

        def load_markets(self):
            return {"BTC/USDT:USDT": {"limits": {"amount": {}}, "precision": {"amount": 8}}}

        def fetch_positions(self, symbols):
            if self.remaining <= 0:
                return []
            return [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": self.remaining, "markPrice": 99.0}]

        def create_order(self, **kwargs):
            self.close_amounts.append(kwargs["amount"])
            self.remaining = 0.25 if len(self.close_amounts) == 1 else 0.0
            return {
                "id": f"close-{len(self.close_amounts)}",
                "status": "closed",
                "filled": kwargs["amount"],
                "average": 99.0,
            }

    fake_exchange = FakeExchange()

    result = await exchange_module._close_position(fake_exchange, "BTC/USDT:USDT", position_side="long")

    assert result["status"] == "closed"
    assert result["remaining_contracts"] == 0.0
    assert result["close_attempts"] == 2
    assert result["close_order_ids"] == ["close-1", "close-2"]
    assert fake_exchange.close_amounts[0] == pytest.approx(1.0)
    assert fake_exchange.close_amounts[1] == pytest.approx(0.25)


@pytest.mark.asyncio
async def test_close_position_returns_closed_only_after_flat_confirmation(monkeypatch):
    monkeypatch.setattr(exchange_module.asyncio, "sleep", AsyncMock())

    class FakeExchange:
        id = "binance"
        options = {"defaultType": "future"}

        def __init__(self):
            self.fetch_calls = 0

        def load_markets(self):
            return {"BTC/USDT:USDT": {"limits": {"amount": {}}, "precision": {"amount": 8}}}

        def fetch_positions(self, symbols):
            self.fetch_calls += 1
            if self.fetch_calls == 1:
                return [{"symbol": "BTC/USDT:USDT", "side": "long", "contracts": 1.0, "markPrice": 100.0}]
            return []

        def create_order(self, **kwargs):
            return {"id": "close-1", "status": "closed", "filled": kwargs["amount"], "average": 99.0}

    result = await exchange_module._close_position(FakeExchange(), "BTC/USDT:USDT", position_side="long")

    assert result["status"] == "closed"
    assert result["remaining_contracts"] == 0.0
