"""
Chart Router - dashboard chart data endpoints.
Provides OHLCV, realtime price, indicators, and marker data for the frontend.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from core.auth import get_current_user
from core.database import PositionModel, WebhookEventModel, get_db
from market_data import fetch_ohlcv_history

router = APIRouter(prefix="/api/chart", tags=["Chart"])


class ChartDataRequest(BaseModel):
    ticker: str = Field(default="BTCUSDT", description="Trading pair")
    timeframe: str = Field(default="1h", description="Timeframe")
    days: int = Field(default=30, ge=1, le=365, description="Days of history")


class ChartDataResponse(BaseModel):
    ticker: str
    timeframe: str
    data: list[dict]
    has_more: bool = False


@router.get("/ohlcv/{ticker}")
async def get_chart_ohlcv(
    ticker: str,
    timeframe: str = "1h",
    days: int = 30,
    user: dict = Depends(get_current_user),
):
    """Get OHLCV data for chart rendering."""
    try:
        ohlcv = await fetch_ohlcv_history(ticker, timeframe, days)

        if not ohlcv:
            raise HTTPException(404, f"No data for {ticker}")

        chart_data = []
        for bar in ohlcv:
            ts = bar.get("timestamp")
            if isinstance(ts, str):
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                timestamp_ms = int(dt.timestamp() * 1000)
            else:
                timestamp_ms = int(ts.timestamp() * 1000) if ts else 0

            chart_data.append({
                "time": timestamp_ms // 1000,
                "open": bar.get("open"),
                "high": bar.get("high"),
                "low": bar.get("low"),
                "close": bar.get("close"),
                "volume": bar.get("volume"),
            })

        return {
            "ticker": ticker,
            "timeframe": timeframe,
            "data": chart_data,
        }

    except HTTPException:
        raise
    except Exception as err:
        logger.error(f"[Chart] Failed to get OHLCV: {err}")
        raise HTTPException(500, f"Chart data error: {err}") from err


@router.get("/realtime/{ticker}")
async def get_realtime_price(
    ticker: str,
    user: dict = Depends(get_current_user),
):
    """Get current real-time price for live chart updates."""
    try:
        from market_data import fetch_market_context

        context = await fetch_market_context(ticker)

        return {
            "ticker": ticker,
            "price": context.current_price,
            "volume_24h": context.volume_24h,
            "change_24h_pct": context.price_change_24h,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    except Exception as err:
        logger.error(f"[Chart] Failed to get realtime price: {err}")
        raise HTTPException(500, f"Realtime error: {err}") from err


@router.get("/indicators/{ticker}")
async def get_chart_indicators(
    ticker: str,
    timeframe: str = "1h",
    user: dict = Depends(get_current_user),
):
    """Get technical indicators for chart overlay."""
    try:
        from market_data import fetch_market_context

        context = await fetch_market_context(ticker)

        indicators = {}

        indicators["rsi_1h"] = context.rsi_1h
        indicators["ema_20"] = context.ema_fast
        indicators["ema_50"] = context.ema_slow
        indicators["atr_1h"] = context.atr_pct
        indicators["volume_24h"] = context.volume_24h

        return {
            "ticker": ticker,
            "timeframe": timeframe,
            "indicators": indicators,
        }

    except Exception as err:
        logger.error(f"[Chart] Failed to get indicators: {err}")
        raise HTTPException(500, f"Indicators error: {err}") from err


@router.get("/positions/{ticker}")
async def get_position_markers(
    ticker: str,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Get position markers for chart display."""
    try:
        user_id = user.get("sub") or user.get("id")

        result = await db.execute(
            select(PositionModel)
            .where(PositionModel.user_id == user_id)
            .where(PositionModel.ticker == ticker)
            .where(PositionModel.status.in_(["open", "pending"]))
        )
        positions = result.scalars().all()

        markers = []
        for pos in positions:
            opened_at_utc = pos.opened_at.replace(tzinfo=timezone.utc) if pos.opened_at.tzinfo is None else pos.opened_at.astimezone(timezone.utc)
            markers.append({
                "time": int(opened_at_utc.timestamp()),
                "position": "belowBar" if pos.direction == "long" else "aboveBar",
                "color": "#26a69a" if pos.direction == "long" else "#ef5350",
                "shape": "arrowUp" if pos.direction == "long" else "arrowDown",
                "text": f"{pos.direction.upper()} @ {pos.entry_price:.2f}",
            })

        return {
            "ticker": ticker,
            "markers": markers,
            "count": len(markers),
        }

    except Exception as err:
        logger.error(f"[Chart] Failed to get position markers: {err}")
        raise HTTPException(500, f"Markers error: {err}") from err


@router.get("/signals/{ticker}")
async def get_signal_markers(
    ticker: str,
    days: int = 7,
    db: AsyncSession = Depends(get_db),
    user: dict = Depends(get_current_user),
):
    """Get historical signal markers for chart."""
    try:
        from datetime import timedelta

        from core.utils.datetime import utcnow

        user_id = user.get("sub") or user.get("id")
        since = utcnow() - timedelta(days=days)

        result = await db.execute(
            select(WebhookEventModel)
            .where(WebhookEventModel.user_id == user_id)
            .where(WebhookEventModel.ticker == ticker)
            .where(WebhookEventModel.status == "executed")
            .where(WebhookEventModel.created_at >= since)
            .order_by(WebhookEventModel.created_at.desc())
            .limit(50)
        )
        events = result.scalars().all()

        markers = []
        for event in events:
            markers.append({
                "time": int(event.created_at.replace(tzinfo=timezone.utc).timestamp()),
                "position": "belowBar" if event.direction == "long" else "aboveBar",
                "color": "#4caf50" if event.direction == "long" else "#f44336",
                "shape": "circle",
                "size": 2,
                "text": f"{event.direction.upper()}",
            })

        return {
            "ticker": ticker,
            "markers": markers,
            "count": len(markers),
        }

    except Exception as err:
        logger.error(f"[Chart] Failed to get signal markers: {err}")
        raise HTTPException(500, f"Signal markers error: {err}") from err


@router.get("/config")
async def get_chart_config(
    user: dict = Depends(get_current_user),
):
    """Get chart configuration settings."""
    return {
        "supported_timeframes": ["1m", "5m", "15m", "1h", "4h", "1d"],
        "default_timeframe": "1h",
        "chart_type": "candlestick",
        "show_volume": True,
        "show_indicators": ["rsi", "ema_20", "ema_50"],
        "colors": {
            "up": "#26a69a",
            "down": "#ef5350",
            "volume_up": "#26a69a80",
            "volume_down": "#ef535080",
            "background": "#1e1e1e",
            "text": "#d1d4dc",
            "grid": "#363c4e",
        },
        "websocket_url": "/ws/prices",
    }
