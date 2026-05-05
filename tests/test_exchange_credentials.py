from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import exchange as exchange_module
from core.config import settings
from models import AIAnalysis, MarketContext, SignalDirection, TakeProfitLevel, TradeDecision, TradingViewSignal
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
        captured.update(kwargs)
        return fake_exchange

    monkeypatch.setattr(exchange_module, "_get_or_create_exchange", fake_get_or_create_exchange)
    return captured


def _assert_empty_credentials(captured: dict):
    assert captured["exchange_id"] == "okx"
    assert captured["api_key"] == ""
    assert captured["api_secret"] == ""
    assert captured["password"] == ""
    assert captured["sandbox"] is True
    assert captured["market_type"] == "contract"


@pytest.mark.asyncio
async def test_execute_trade_does_not_fallback_to_global_credentials_for_user(monkeypatch):
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

    config = result["captured_exchange_config"]
    assert config["exchange"] == "okx"
    assert config["api_key"] == ""
    assert config["api_secret"] == ""
    assert config["password"] == ""
    assert config["live_trading"] is True
    assert config["sandbox_mode"] is True
    assert config["limit_timeout_overrides"] == {"1h": 3600}


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
        {"exchange": {"name": "okx", "limit_timeout_overrides": {}, "live_trading": True}},
    )

    config = result["captured_exchange_config"]
    assert config["limit_timeout_overrides"] == {}


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
        AsyncMock(return_value={"id": "entry-1", "status": "closed", "average": 100.0}),
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
async def test_execute_trade_pending_limit_includes_exit_plan(monkeypatch):
    _set_global_exchange_defaults(monkeypatch)
    monkeypatch.setattr(exchange_module, "_CCXT_AVAILABLE", True)

    fake_exchange = SimpleNamespace(options={"defaultType": "future"})
    _capture_exchange_kwargs(monkeypatch, fake_exchange)

    monkeypatch.setattr(exchange_module, "_resolve_symbol", lambda *args, **kwargs: "BTC/USDT:USDT")
    monkeypatch.setattr(
        exchange_module,
        "_create_exchange_order",
        AsyncMock(return_value={"id": "entry-1", "status": "open", "filled": 0.0, "price": 100.0}),
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
        ),
        _user_exchange_config(),
    )

    assert result["status"] == "pending"
    assert result["stop_loss"] == 98.0
    assert result["take_profit_orders"][0]["price"] == 103.0
    assert result["take_profit_orders"][0]["status"] == "pending"


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
    elif call_name == "get_open_positions":
        result = await exchange_module.get_open_positions(exchange_config)
        result = result[0]
    else:
        result = await exchange_module.get_recent_orders("BTCUSDT", 10, exchange_config)
        result = result[0]

    assert result[expected_key] == expected_value
    _assert_empty_credentials(captured)


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
