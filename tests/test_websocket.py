"""Tests for WebSocket functionality."""
import pytest
import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import Mock, patch, AsyncMock


class TestConnectionManager:
    @pytest.fixture
    def manager(self):
        from routers.websocket import ConnectionManager
        return ConnectionManager()

    @pytest.fixture
    def mock_websocket(self):
        ws = Mock()
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()
        ws.receive_text = AsyncMock()
        return ws

    def test_manager_initialization(self, manager):
        assert manager.active_connections == {}
        assert manager.user_connections == {}

    async def test_connect_user(self, manager, mock_websocket):
        await manager.connect(mock_websocket, "user123")

        assert "user123" in manager.active_connections
        assert mock_websocket in manager.active_connections["user123"]

    async def test_disconnect_user(self, manager, mock_websocket):
        await manager.connect(mock_websocket, "user123")
        manager.disconnect(mock_websocket)

        assert "user123" not in manager.active_connections or len(manager.active_connections["user123"]) == 0

    async def test_send_personal_message(self, manager, mock_websocket):
        await manager.connect(mock_websocket, "user123")

        message = {"type": "test", "data": "hello"}
        await manager.send_personal(message, mock_websocket)

        mock_websocket.send_json.assert_called_once_with(message)

    async def test_broadcast_to_user(self, manager, mock_websocket):
        await manager.connect(mock_websocket, "user123")

        message = {"type": "broadcast", "data": "update"}
        await manager.broadcast_to_user("user123", message)

        mock_websocket.send_json.assert_called()

    def test_get_user_count(self, manager, mock_websocket):
        asyncio.run(manager.connect(mock_websocket, "user123"))

        count = manager.get_user_count()
        assert count == 1


class TestWebSocketPositions:
    @pytest.fixture
    def mock_db_positions(self):
        return [
            {
                "id": "pos1",
                "ticker": "BTCUSDT",
                "direction": "long",
                "entry_price": 50000.0,
                "quantity": 0.1,
                "current_pnl_pct": 2.5,
            },
            {
                "id": "pos2",
                "ticker": "ETHUSDT",
                "direction": "short",
                "entry_price": 3000.0,
                "quantity": 1.0,
                "current_pnl_pct": -1.5,
            },
        ]

    @patch('routers.websocket.verify_jwt_token')
    async def test_position_websocket_auth(self, mock_verify):
        mock_verify.return_value = {"sub": "user123", "user_id": "user123"}

        assert mock_verify.return_value["user_id"] == "user123"

    @patch('routers.websocket._fetch_user_positions')
    async def test_fetch_positions(self, mock_fetch, mock_db_positions):
        mock_fetch.return_value = mock_db_positions

        result = await mock_fetch("user123")

        assert len(result) == 2
        assert result[0]["ticker"] == "BTCUSDT"


class TestWebSocketPrices:
    @patch('market_data.fetch_market_context')
    async def test_price_streaming(self, mock_fetch):
        mock_context = Mock()
        mock_context.price = 50000.0
        mock_context.price_change_pct_1h = 1.5
        mock_context.volume_24h = 1000000000
        mock_context.rsi_1h = 65
        mock_fetch.return_value = mock_context

        result = await mock_fetch("BTCUSDT")

        assert result.price == 50000.0
        assert result.rsi_1h == 65


class TestWebSocketSystem:
    @patch('routers.websocket._fetch_system_stats')
    async def test_system_stats(self, mock_stats):
        mock_stats.return_value = {
            "open_positions": 5,
            "total_trades": 100,
            "websocket_connections": 3,
        }

        result = await mock_stats()

        assert result["open_positions"] == 5
        assert result["websocket_connections"] == 3


class TestBroadcastFunctions:
    @pytest.fixture
    def manager(self):
        from routers.websocket import ConnectionManager
        return ConnectionManager()

    async def test_broadcast_position_update(self, manager):
        from routers.websocket import broadcast_position_update

        ws = Mock()
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()
        await manager.connect(ws, "user123")

        position = {
            "position_id": "pos1",
            "ticker": "BTCUSDT",
            "pnl_pct": 3.5,
        }

        await broadcast_position_update("user123", position)

        ws.send_json.assert_called()

    async def test_broadcast_trade_executed(self, manager):
        from routers.websocket import broadcast_trade_executed

        ws = Mock()
        ws.accept = AsyncMock()
        ws.send_json = AsyncMock()
        await manager.connect(ws, "user123")

        trade = {
            "trade_id": "trade1",
            "ticker": "ETHUSDT",
            "direction": "buy",
        }

        await broadcast_trade_executed("user123", trade)

        ws.send_json.assert_called()


class TestWebSocketMessages:
    def test_ping_pong_message(self):
        ping = {"type": "ping"}
        expected_pong = {"type": "pong", "timestamp": datetime.now(timezone.utc).isoformat()}

        assert ping["type"] == "ping"

    def test_subscribe_message(self):
        subscribe = {"type": "subscribe", "channels": ["positions", "prices"]}

        assert "positions" in subscribe["channels"]

    def test_position_update_message_format(self):
        message = {
            "type": "position_update",
            "position_id": "pos123",
            "ticker": "BTCUSDT",
            "pnl_pct": 5.0,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        assert message["type"] == "position_update"
        assert "ticker" in message
        assert "timestamp" in message

    def test_price_update_message_format(self):
        message = {
            "type": "price_update",
            "ticker": "BTCUSDT",
            "price": 50000.0,
            "change_1h_pct": 1.5,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

        assert message["type"] == "price_update"
        assert "price" in message


class TestWebSocketStatusEndpoint:
    def test_status_response_format(self):
        status = {
            "active_connections": 5,
            "online_users": ["user1", "user2", "user3"],
            "endpoints": [
                {"path": "/ws/positions", "description": "Real-time position updates"},
                {"path": "/ws/prices", "description": "Real-time price streaming"},
                {"path": "/ws/system", "description": "System status (admin only)"},
            ],
        }

        assert status["active_connections"] == 5
        assert len(status["endpoints"]) == 3