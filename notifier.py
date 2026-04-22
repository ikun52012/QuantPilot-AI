"""
QuantPilot AI - Telegram Notifications
Sends trading notifications to Telegram.
"""
import httpx
from loguru import logger
from core.config import settings
from models import TradeDecision, AIAnalysis


async def send_telegram(text: str):
    """Send a message to Telegram."""
    if not settings.telegram.bot_token or not settings.telegram.chat_id:
        logger.debug("[Telegram] Not configured, skipping notification")
        return

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"https://api.telegram.org/bot{settings.telegram.bot_token}/sendMessage",
                json={
                    "chat_id": settings.telegram.chat_id,
                    "text": text,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            resp.raise_for_status()
            logger.debug("[Telegram] Message sent")
    except Exception as e:
        logger.error(f"[Telegram] Failed to send message: {e}")


async def notify_signal_received(ticker: str, direction: str, price: float):
    """Notify when a new signal is received."""
    text = (
        f"馃摗 <b>Signal Received</b>\n"
        f"鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣\n"
        f"Ticker: <code>{ticker}</code>\n"
        f"Direction: <b>{direction.upper()}</b>\n"
        f"Price: <code>{price}</code>\n"
        f"Status: Analyzing..."
    )
    await send_telegram(text)


async def notify_pre_filter_blocked(ticker: str, direction: str, reason: str):
    """Notify when a signal is blocked by pre-filter."""
    text = (
        f"馃毇 <b>Signal Blocked (Pre-Filter)</b>\n"
        f"鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣\n"
        f"Ticker: <code>{ticker}</code>\n"
        f"Direction: {direction.upper()}\n"
        f"Reason: {reason}"
    )
    await send_telegram(text)


async def notify_ai_analysis(ticker: str, analysis: AIAnalysis):
    """Notify AI analysis results."""
    emoji_map = {
        "execute": "鉁?,
        "modify": "馃攧",
        "reject": "鉂?,
        "hold": "鈴革笍",
    }
    emoji = emoji_map.get(analysis.recommendation, "鉂?)

    warnings_text = ""
    if analysis.warnings:
        warnings_text = "\n鈿狅笍 Warnings:\n" + "\n".join(f"  鈥?{w}" for w in analysis.warnings)

    text = (
        f"{emoji} <b>AI Analysis: {analysis.recommendation.upper()}</b>\n"
        f"鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣\n"
        f"Ticker: <code>{ticker}</code>\n"
        f"Confidence: <b>{analysis.confidence:.0%}</b>\n"
        f"Risk Score: {analysis.risk_score:.0%}\n"
        f"Market: {analysis.market_condition}\n"
        f"Position Size: {analysis.position_size_pct:.0%}\n"
        f"\n馃挰 {analysis.reasoning}"
        f"{warnings_text}"
    )
    await send_telegram(text)


async def notify_trade_executed(decision: TradeDecision, order_result: dict):
    """Notify when a trade is executed."""
    status = order_result.get("status", "unknown")
    if status in ("filled", "simulated"):
        mode = "馃摑 PAPER" if status == "simulated" else "馃挵 LIVE"
        text = (
            f"馃幆 <b>Trade Executed</b> ({mode})\n"
            f"鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣\n"
            f"Ticker: <code>{decision.ticker}</code>\n"
            f"Direction: <b>{decision.direction.value.upper() if decision.direction else 'N/A'}</b>\n"
            f"Entry: <code>{decision.entry_price}</code>\n"
            f"Stop Loss: <code>{decision.stop_loss}</code>\n"
            f"Take Profit: <code>{decision.take_profit}</code>\n"
            f"Quantity: <code>{decision.quantity}</code>"
        )
        if decision.ai_analysis:
            text += f"\n馃 AI Confidence: {decision.ai_analysis.confidence:.0%}"
    elif status == "error":
        text = (
            f"馃挜 <b>Trade Failed</b>\n"
            f"鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣\n"
            f"Ticker: <code>{decision.ticker}</code>\n"
            f"Error: {order_result.get('reason', 'Unknown')}"
        )
    else:
        text = f"鈩癸笍 Trade status: {status} - {order_result.get('reason', '')}"

    await send_telegram(text)


async def notify_error(error: str):
    """Notify about system errors."""
    text = f"馃敶 <b>System Error</b>\n鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣\n{error}"
    await send_telegram(text)


async def notify_daily_summary(trades: int, win_rate: float, pnl: float):
    """Send daily trading summary."""
    emoji = "馃搱" if pnl >= 0 else "馃搲"
    text = (
        f"{emoji} <b>Daily Summary</b>\n"
        f"鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣鈹佲攣\n"
        f"Total Trades: {trades}\n"
        f"Win Rate: {win_rate:.1f}%\n"
        f"P&L: <b>{pnl:+.2f}%</b>"
    )
    await send_telegram(text)
