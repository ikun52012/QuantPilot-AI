"""
WebSocket Router for Real-time Data Streaming.
Provides live position updates, price alerts, and system status.
"""
import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Optional
from collections import defaultdict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from loguru import logger
import inspect

from core.auth import verify_token, require_admin
from core.config import settings
from core.database import db_manager, get_user_by_id


router = APIRouter(tags=["WebSocket"])
verify_jwt_token = verify_token

# WebSocket rate limiting
_WS_CONNECTION_LIMIT_PER_USER = 5  # Max 5 connections per user
_WS_CONNECTION_COOLDOWN = 60  # 60 seconds cooldown between connections
_ws_connection_times: dict[str, list[float]] = defaultdict(list)


def _verify_ws_token_or_none(token: str) -> Optional[dict]:
    """Reject expired, invalid, or still-pending-2FA tokens for WebSockets."""
    payload = verify_token(token)
    if not payload or payload.get("2fa_pending"):
        return None
    return payload


async def _authenticate_ws_user_or_none(token: str, require_admin_role: bool = False) -> Optional[dict]:
    """Validate the token and current user state before opening a socket."""
    payload = _verify_ws_token_or_none(token)
    if not payload:
        return None

    user_id = payload.get("sub") or payload.get("user_id")
    if not user_id or not db_manager.async_session_factory:
        return None

    try:
        async with db_manager.async_session_factory() as session:
            user = await get_user_by_id(session, user_id)
    except Exception as e:
        logger.debug(f"[WebSocket] User lookup failed: {e}")
        return None

    if not user or not user.is_active:
        return None
    if int(payload.get("ver", 0)) != int(user.token_version or 0):
        return None
    if require_admin_role and user.role != "admin":
        return None

    return {
        "sub": user.id,
        "username": user.username,
        "role": user.role,
        "email": user.email,
    }


def _ws_message(msg_type: str, data: dict, ticker: Optional[str] = None) -> dict:
    """Create standardized WebSocket message format."""
    message = {
        "type": msg_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if ticker:
        message["ticker"] = ticker
    message.update(data)
    return message


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = defaultdict(list)
        self.user_connections: dict[WebSocket, str] = {}
        self._broadcast_task = None

    async def connect(self, websocket: WebSocket, user_id: str):
        # Rate limiting check
        now = time.time()
        user_connections = _ws_connection_times[user_id]

        # Remove old connection times (older than cooldown period)
        user_connections = [t for t in user_connections if now - t < _WS_CONNECTION_COOLDOWN]
        _ws_connection_times[user_id] = user_connections

        # Check connection limit
        if len(user_connections) >= _WS_CONNECTION_LIMIT_PER_USER:
            close_result = websocket.close(code=4029, reason="Too many connections. Please wait.")
            if inspect.isawaitable(close_result):
                await close_result
            logger.warning(f"[WebSocket] Rate limit exceeded for user {user_id}")
            return False

        await websocket.accept()
        self.active_connections[user_id].append(websocket)
        self.user_connections[websocket] = user_id
        user_connections.append(now)
        logger.info(f"[WebSocket] User {user_id} connected ({len(user_connections)}/{_WS_CONNECTION_LIMIT_PER_USER})")
        return True

    def disconnect(self, websocket: WebSocket):
        user_id = self.user_connections.get(websocket)
        if user_id:
            if websocket in self.active_connections[user_id]:
                self.active_connections[user_id].remove(websocket)
            del self.user_connections[websocket]
            logger.info(f"[WebSocket] User {user_id} disconnected")

    async def send_personal(self, message: dict, websocket: WebSocket):
        try:
            await websocket.send_json(message)
        except Exception as e:
            logger.debug(f"[WebSocket] Send failed: {e}")

    async def broadcast_to_user(self, user_id: str, message: dict):
        connections = self.active_connections.get(user_id, [])
        for connection in connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass

    async def broadcast_all(self, message: dict):
        for user_id, connections in self.active_connections.items():
            for connection in connections:
                try:
                    await connection.send_json(message)
                except Exception:
                    pass

    def get_user_count(self) -> int:
        return len(self.user_connections)

    def get_online_users(self) -> list[str]:
        return list(self.active_connections.keys())


manager = ConnectionManager()


@router.websocket("/ws/positions")
async def websocket_positions(websocket: WebSocket):
    """
    WebSocket endpoint for real-time position updates.

    Messages sent:
    - position_update: {position_id, ticker, pnl_pct, current_price, ...}
    - position_closed: {position_id, exit_reason, pnl_pct}
    - trade_executed: {trade_id, ticker, direction, entry_price}
    """
    user_id = None

    try:
        token = websocket.query_params.get("token")
        if not token:
            await websocket.close(code=4001, reason="Missing authentication token")
            return

        payload = await _authenticate_ws_user_or_none(token)
        if not payload:
            await websocket.close(code=4001, reason="Invalid token")
            return

        user_id = payload.get("sub") or payload.get("user_id")
        if not user_id:
            await websocket.close(code=4001, reason="Invalid token payload")
            return

        if not await manager.connect(websocket, user_id):
            return

        await manager.send_personal({
            "type": "connected",
            "user_id": user_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "message": "WebSocket connected successfully",
        }, websocket)

        while True:
            data = await websocket.receive_text()

            try:
                message = json.loads(data)
                msg_type = message.get("type", "unknown")

                if msg_type == "ping":
                    await manager.send_personal({
                        "type": "pong",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }, websocket)

                elif msg_type == "subscribe":
                    channels = message.get("channels", ["positions"])
                    await manager.send_personal({
                        "type": "subscribed",
                        "channels": channels,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }, websocket)

                elif msg_type == "unsubscribe":
                    channels = message.get("channels", [])
                    await manager.send_personal({
                        "type": "unsubscribed",
                        "channels": channels,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }, websocket)

                elif msg_type == "get_positions":
                    positions = await _fetch_user_positions(user_id)
                    await manager.send_personal({
                        "type": "positions_list",
                        "positions": positions,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }, websocket)

                else:
                    await manager.send_personal({
                        "type": "error",
                        "message": f"Unknown message type: {msg_type}",
                    }, websocket)

            except json.JSONDecodeError:
                await manager.send_personal({
                    "type": "error",
                    "message": "Invalid JSON format",
                }, websocket)

    except WebSocketDisconnect:
        manager.disconnect(websocket)

    except Exception as e:
        logger.error(f"[WebSocket] Error: {e}")
        manager.disconnect(websocket)


@router.websocket("/ws/prices")
async def websocket_prices(websocket: WebSocket):
    """
    WebSocket endpoint for real-time price streaming.

    Messages sent:
    - price_update: {ticker, price, change_pct, volume}
    - price_alert: {ticker, price, alert_type}
    """
    user_id = None

    try:
        token = websocket.query_params.get("token")
        if not token:
            await websocket.close(code=4001, reason="Missing authentication token")
            return

        payload = await _authenticate_ws_user_or_none(token)
        if not payload:
            await websocket.close(code=4001, reason="Invalid token")
            return

        user_id = payload.get("sub") or payload.get("user_id")

        if not await manager.connect(websocket, f"prices_{user_id}"):
            return

        subscribed_tickers = set()

        await manager.send_personal({
            "type": "connected",
            "message": "Price WebSocket connected",
        }, websocket)

        price_task = None

        try:
            while True:
                data = await websocket.receive_text()

                try:
                    message = json.loads(data)
                    msg_type = message.get("type")

                    if msg_type == "subscribe_tickers":
                        tickers = message.get("tickers", [])
                        subscribed_tickers.update(tickers)

                        if subscribed_tickers and not price_task:
                            price_task = asyncio.create_task(
                                _stream_prices(websocket, subscribed_tickers)
                            )

                        await manager.send_personal({
                            "type": "subscribed_tickers",
                            "tickers": list(subscribed_tickers),
                        }, websocket)

                    elif msg_type == "unsubscribe_tickers":
                        tickers = message.get("tickers", [])
                        subscribed_tickers.difference_update(tickers)

                        if not subscribed_tickers and price_task:
                            price_task.cancel()
                            try:
                                await price_task
                            except asyncio.CancelledError:
                                pass
                            price_task = None

                        await manager.send_personal({
                            "type": "unsubscribed_tickers",
                            "tickers": list(subscribed_tickers),
                        }, websocket)

                    elif msg_type == "ping":
                        await manager.send_personal({"type": "pong"}, websocket)

                except json.JSONDecodeError:
                    pass
        finally:
            if price_task:
                price_task.cancel()
                try:
                    await price_task
                except asyncio.CancelledError:
                    pass

    except WebSocketDisconnect:
        manager.disconnect(websocket)

    except Exception as e:
        logger.error(f"[WebSocket/Prices] Error: {e}")
        manager.disconnect(websocket)


@router.websocket("/ws/system")
async def websocket_system(websocket: WebSocket):
    """
    WebSocket endpoint for system status and alerts.

    Messages sent:
    - system_status: {status, uptime, active_positions, ...}
    - risk_alert: {type, message, severity}
    - webhook_received: {ticker, direction, timestamp}
    """
    user_id = None

    try:
        token = websocket.query_params.get("token")
        if not token:
            await websocket.close(code=4001, reason="Missing authentication token")
            return

        payload = await _authenticate_ws_user_or_none(token, require_admin_role=True)
        if not payload:
            await websocket.close(code=4001, reason="Invalid token")
            return

        user_id = payload.get("sub") or payload.get("user_id")

        if not await manager.connect(websocket, f"system_{user_id}"):
            return

        await manager.send_personal({
            "type": "connected",
            "role": "admin",
            "message": "System WebSocket connected",
        }, websocket)

        status_task = asyncio.create_task(_stream_system_status(websocket))

        try:
            while True:
                data = await websocket.receive_text()

                try:
                    message = json.loads(data)
                    msg_type = message.get("type")

                    if msg_type == "ping":
                        await manager.send_personal({"type": "pong"}, websocket)

                    elif msg_type == "get_stats":
                        stats = await _fetch_system_stats()
                        await manager.send_personal({
                            "type": "system_stats",
                            **stats,
                        }, websocket)

                except json.JSONDecodeError:
                    pass

        finally:
            status_task.cancel()

    except WebSocketDisconnect:
        manager.disconnect(websocket)

    except Exception as e:
        logger.error(f"[WebSocket/System] Error: {e}")
        manager.disconnect(websocket)


@router.get("/ws/status")
async def websocket_status(admin: dict = Depends(require_admin)):
    """Get WebSocket connection status."""
    return {
        "active_connections": manager.get_user_count(),
        "online_users": manager.get_online_users(),
        "endpoints": [
            {"path": "/ws/positions", "description": "Real-time position updates"},
            {"path": "/ws/prices", "description": "Real-time price streaming"},
            {"path": "/ws/system", "description": "System status (admin only)"},
        ],
    }


async def _fetch_user_positions(user_id: str) -> list[dict]:
    """Fetch user's open positions from database."""
    try:
        from core.database import db_manager, PositionModel
        from sqlalchemy import select

        async with db_manager.async_session_factory() as session:
            result = await session.execute(
                select(PositionModel)
                .where(PositionModel.user_id == user_id)
                .where(PositionModel.status.in_(["open", "pending"]))
            )
            positions = result.scalars().all()

            return [
                {
                    "id": p.id,
                    "ticker": p.ticker,
                    "direction": p.direction,
                    "status": p.status,
                    "entry_price": p.entry_price,
                    "quantity": p.quantity,
                    "remaining_quantity": p.remaining_quantity,
                    "stop_loss": p.stop_loss,
                    "take_profit_json": p.take_profit_json,
                    "current_pnl_pct": p.current_pnl_pct,
                    "unrealized_pnl_usdt": p.unrealized_pnl_usdt,
                    "opened_at": p.opened_at.isoformat() if p.opened_at else None,
                    "strategy_name": p.strategy_name,
                }
                for p in positions
            ]

    except Exception as e:
        logger.debug(f"[WebSocket] Failed to fetch positions: {e}")
        return []


async def _stream_prices(websocket: WebSocket, tickers: set[str]):
    """Stream prices for subscribed tickers."""
    from market_data import fetch_market_context

    while True:
        try:
            for ticker in tickers:
                try:
                    context = await fetch_market_context(ticker)

                    await manager.send_personal({
                        "type": "price_update",
                        "ticker": ticker,
                        "price": context.current_price,
                        "change_1h_pct": context.price_change_1h,
                        "volume_24h": context.volume_24h,
                        "rsi_1h": context.rsi_1h,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }, websocket)

                except Exception as e:
                    logger.debug(f"[WebSocket/Prices] Failed for {ticker}: {e}")

            await asyncio.sleep(5)

        except asyncio.CancelledError:
            break

        except Exception as e:
            logger.error(f"[WebSocket/Prices] Stream error: {e}")
            await asyncio.sleep(10)


async def _stream_system_status(websocket: WebSocket):
    """Stream system status periodically."""
    while True:
        try:
            stats = await _fetch_system_stats()

            await manager.send_personal({
                "type": "system_status",
                **stats,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }, websocket)

            await asyncio.sleep(30)

        except asyncio.CancelledError:
            break

        except Exception as e:
            logger.error(f"[WebSocket/System] Status stream error: {e}")
            await asyncio.sleep(60)


async def _fetch_system_stats() -> dict:
    """Fetch system statistics."""
    try:
        from core.database import db_manager, PositionModel, TradeModel, UserModel
        from sqlalchemy import select, func

        async with db_manager.async_session_factory() as session:
            open_positions = await session.execute(
                select(func.count(PositionModel.id))
                .where(PositionModel.status.in_(["open", "pending"]))
            )
            open_count = open_positions.scalar() or 0

            today_trades = await session.execute(
                select(func.count(TradeModel.id))
            )
            trades_count = today_trades.scalar() or 0

            active_users = await session.execute(
                select(func.count(UserModel.id))
                .where(UserModel.is_active == True)
            )
            users_count = active_users.scalar() or 0

            return {
                "open_positions": open_count,
                "total_trades": trades_count,
                "active_users": users_count,
                "websocket_connections": manager.get_user_count(),
                "trading_mode": "live" if settings.exchange.live_trading else "paper",
                "enhanced_filters": bool(getattr(settings, "enhanced_filters_enabled", True)),
            }

    except Exception as e:
        logger.debug(f"[WebSocket] Failed to fetch stats: {e}")
        return {
            "open_positions": 0,
            "websocket_connections": manager.get_user_count(),
        }


async def broadcast_position_update(user_id: str, position: dict):
    """Broadcast position update to user's WebSocket connections."""
    await manager.broadcast_to_user(user_id, {
        "type": "position_update",
        **position,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def broadcast_trade_executed(user_id: str, trade: dict):
    """Broadcast trade execution to user's WebSocket connections."""
    await manager.broadcast_to_user(user_id, {
        "type": "trade_executed",
        **trade,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def broadcast_position_closed(user_id: str, position: dict):
    """Broadcast position closure to user's WebSocket connections."""
    await manager.broadcast_to_user(user_id, {
        "type": "position_closed",
        **position,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def broadcast_risk_alert(user_id: str, alert: dict):
    """Broadcast risk alert to user's WebSocket connections."""
    await manager.broadcast_to_user(user_id, {
        "type": "risk_alert",
        **alert,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


async def broadcast_webhook_received(admin_id: str, webhook: dict):
    """Broadcast webhook received event to admin connections."""
    await manager.broadcast_to_user(f"system_{admin_id}", {
        "type": "webhook_received",
        **webhook,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
